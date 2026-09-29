"""视频库扫描与偏好数据持久化。

模块职责:
  - 递归扫描一个或多个目录, 登记视频文件
  - 用 SQLite 长期保存每个视频的「喜欢 / 不喜欢」记录
  - 计算 Wilson 评分下界, 用于长期偏好排序
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTS = {
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".flv",
    ".wmv", ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".3gp", ".ogv",
    ".rmvb", ".rm", ".vob", ".asf", ".f4v", ".divx",
}

# Wilson 评分区间 z=1.96 对应约 95% 置信度
WILSON_Z = 1.96

_NATURAL_SPLIT = re.compile(r"(\d+)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id         TEXT PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,
    root       TEXT NOT NULL,
    rel_path   TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mtime      REAL NOT NULL,
    added_at   REAL NOT NULL,
    last_seen  REAL NOT NULL,
    duration   REAL,
    direct     INTEGER,
    media_mtime REAL,
    width      INTEGER,
    height     INTEGER,
    vcodec     TEXT,
    acodec     TEXT
);

CREATE TABLE IF NOT EXISTS votes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id   TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('like', 'dislike')),
    client_id  TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_votes_video  ON votes (video_id);
CREATE INDEX IF NOT EXISTS idx_votes_client ON votes (client_id, video_id, id);
"""

# 视频文件变了就清空旧的探测结果, 交给下次访问时重新 probe
MEDIA_COLS = ("duration", "direct", "media_mtime", "width", "height", "vcodec", "acodec")

UPSERT_VIDEO = """
INSERT INTO videos (id, path, root, rel_path, size, mtime, added_at, last_seen)
VALUES (:id, :path, :root, :rel_path, :size, :mtime, :added_at, :last_seen)
ON CONFLICT(id) DO UPDATE SET
    path       = excluded.path,
    rel_path   = excluded.rel_path,
    size       = excluded.size,
    -- 以下表达式读的是 videos.mtime 的旧值, 必须排在 mtime 赋值之前
    duration   = CASE WHEN videos.mtime = excluded.mtime THEN videos.duration   ELSE NULL END,
    direct     = CASE WHEN videos.mtime = excluded.mtime THEN videos.direct     ELSE NULL END,
    media_mtime= CASE WHEN videos.mtime = excluded.mtime THEN videos.media_mtime ELSE NULL END,
    width      = CASE WHEN videos.mtime = excluded.mtime THEN videos.width      ELSE NULL END,
    height     = CASE WHEN videos.mtime = excluded.mtime THEN videos.height     ELSE NULL END,
    vcodec     = CASE WHEN videos.mtime = excluded.mtime THEN videos.vcodec     ELSE NULL END,
    acodec     = CASE WHEN videos.mtime = excluded.mtime THEN videos.acodec     ELSE NULL END,
    mtime      = excluded.mtime,
    last_seen  = excluded.last_seen
"""


def video_id_for(path: str | os.PathLike[str]) -> str:
    """由绝对路径派生稳定 ID (sha1 前 16 位)。"""
    raw = os.path.realpath(path).encode("utf-8", "surrogateescape")
    return hashlib.sha1(raw).hexdigest()[:16]


def natural_key(text: str) -> tuple:
    """让 a2.mp4 排在 a10.mp4 前面。"""
    parts = _NATURAL_SPLIT.split(text)
    return tuple(
        (1, int(p)) if p.isdigit() else (0, p.lower())
        for p in parts
        if p != ""
    )


def wilson_lower(likes: int, dislikes: int, z: float = WILSON_Z) -> float:
    """Wilson 评分区间下界。

    相比 ``like / (like + dislike)``, 它会惩罚样本量过小的视频:
    只有 1 次喜欢的视频得分远低于 30 次里 25 次喜欢的视频,
    长期累积后排序才有参考价值。无记录返回 0.0。
    """
    n = likes + dislikes
    if n <= 0:
        return 0.0
    p = likes / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def preference_label(likes: int, dislikes: int, score: float) -> str:
    """把累计数据翻译成一句结论。"""
    n = likes + dislikes
    if n == 0:
        return "未标记"
    if dislikes == 0 and likes > 0:
        return "全喜欢" if likes >= 3 else "喜欢"
    if likes == 0:
        return "不喜欢" if dislikes >= 3 else "不太喜欢"
    if score >= 0.6 and likes >= 3:
        return "最喜欢"
    if score >= 0.4:
        return "比较喜欢"
    if score < 0.2:
        return "不太喜欢"
    return "一般"


@dataclass
class Video:
    id: str
    path: str
    root: str
    rel_path: str
    size: int
    mtime: float
    added_at: float
    last_seen: float
    duration: float | None = None
    direct: int | None = None
    media_mtime: float | None = None
    width: int | None = None
    height: int | None = None
    vcodec: str | None = None
    acodec: str | None = None
    like: int = 0
    dislike: int = 0
    my_like: int = 0
    my_dislike: int = 0
    last_vote_at: float | None = None

    @property
    def title(self) -> str:
        return Path(self.rel_path).stem

    @property
    def wilson(self) -> float:
        return wilson_lower(self.like, self.dislike)

    def to_json(self) -> dict:
        score = self.wilson
        return {
            "id": self.id,
            "title": self.title,
            "rel_path": self.rel_path,
            "size": self.size,
            "mtime": self.mtime,
            "stream_url": f"/media/{self.id}",
            "like": self.like,
            "dislike": self.dislike,
            "my_like": self.my_like,
            "my_dislike": self.my_dislike,
            "wilson": round(score, 4),
            "label": preference_label(self.like, self.dislike, score),
            "last_vote_at": self.last_vote_at,
            "duration": self.duration,
            "direct": None if self.direct is None else bool(self.direct),
            "probed": self.media_mtime is not None,
            "width": self.width,
            "height": self.height,
            "vcodec": self.vcodec,
            "acodec": self.acodec,
        }


class Library:
    """视频目录 + SQLite 偏好记录。所有方法都在调用时自行开/关连接。"""

    def __init__(self, roots: list[str], db_path: str) -> None:
        self.roots = [os.path.realpath(r) for r in roots]
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
        # id -> 绝对路径, 只服务扫描到的文件, 天然避免任意路径读取
        self._paths: dict[str, str] = {}

    # ------------------------------------------------------------------ db

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # --------------------------------------------------------------- 扫描

    def scan(self) -> dict:
        """递归登记所有视频文件, 返回本次统计。"""
        now = time.time()
        found: dict[str, Video] = {}
        seen_real: set[str] = set()

        for root in self.roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames.sort()
                for name in sorted(filenames):
                    if Path(name).suffix.lower() not in VIDEO_EXTS:
                        continue
                    if name.startswith("."):
                        continue
                    full = os.path.join(dirpath, name)
                    real = os.path.realpath(full)
                    if real in seen_real:
                        continue
                    seen_real.add(real)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    vid = video_id_for(real)
                    rel = os.path.relpath(full, root)
                    found[vid] = Video(
                        id=vid,
                        path=real,
                        root=root,
                        rel_path=rel,
                        size=st.st_size,
                        mtime=st.st_mtime,
                        added_at=now,
                        last_seen=now,
                    )

        with self.connect() as conn:
            before = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
            known = {
                row["id"]: (row["size"], row["mtime"])
                for row in conn.execute("SELECT id, size, mtime FROM videos")
            }

            conn.executemany(UPSERT_VIDEO, [dict(v.__dict__) for v in found.values()])

            # 本次没扫到的旧条目, 和本次新冒出来的条目
            stale = {i: s for i, s in known.items() if i not in found}
            fresh = [vid for vid in found if vid not in known]

            # 文件被改名或挪了目录: 大小和修改时间都没变, 认为是同一个文件,
            # 把累积的喜欢/不喜欢记录搬到新 id 上, 否则整理一次目录就白记了
            renamed = 0
            for vid in list(fresh):
                size, mtime = found[vid].size, found[vid].mtime
                for old_id, (old_size, old_mtime) in list(stale.items()):
                    if (old_size, old_mtime) != (size, mtime):
                        continue
                    conn.execute(
                        "UPDATE votes SET video_id = ? WHERE video_id = ?", (vid, old_id)
                    )
                    conn.execute("DELETE FROM videos WHERE id = ?", (old_id,))
                    stale.pop(old_id)
                    fresh.remove(vid)
                    renamed += 1
                    break

            # 剩下的就是真被删掉的文件
            conn.executemany("DELETE FROM videos WHERE id = ?", [(i,) for i in stale])
            after = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]

        self._paths = {v.id: v.path for v in found.values()}
        return {
            "roots": self.roots,
            "total": len(found),
            "added": after - before,
            "removed": len(stale),
            "renamed": renamed,
        }

    def path_of(self, vid: str) -> str | None:
        return self._paths.get(vid)

    def media_info(self, vid: str) -> dict | None:
        """取缓存的 ffprobe 结果; 文件被改过就当作没有缓存。"""
        row = self._one(
            """SELECT duration, direct, width, height, vcodec, acodec, mtime, media_mtime
               FROM videos WHERE id = ?""",
            (vid,),
        )
        if row is None or row["media_mtime"] is None or row["media_mtime"] != row["mtime"]:
            return None
        return dict(row)

    def save_media_info(self, vid: str, info) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE videos
                   SET duration = ?, direct = ?, media_mtime = mtime,
                       width = ?, height = ?, vcodec = ?, acodec = ?
                   WHERE id = ?""",
                (
                    round(info.duration, 3), 1 if info.direct else 0,
                    info.width, info.height, info.vcodec, info.acodec, vid,
                ),
            )

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    # --------------------------------------------------------------- 读取

    def list_videos(self, sort: str = "folder", client_id: str = "") -> list[Video]:
        """返回带累计数据的视频列表, 按指定模式排序。"""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT v.*,
                          COALESCE(t.like, 0)    AS like,
                          COALESCE(t.dislike, 0) AS dislike,
                          t.last_vote_at        AS last_vote_at,
                          COALESCE(m.like, 0)    AS my_like,
                          COALESCE(m.dislike, 0) AS my_dislike
                   FROM videos v
                   LEFT JOIN (
                       SELECT video_id,
                              SUM(action = 'like')    AS like,
                              SUM(action = 'dislike') AS dislike,
                              MAX(created_at)        AS last_vote_at
                       FROM votes GROUP BY video_id
                   ) t ON t.video_id = v.id
                   LEFT JOIN (
                       SELECT video_id,
                              SUM(action = 'like')    AS like,
                              SUM(action = 'dislike') AS dislike
                       FROM votes WHERE client_id = :cid GROUP BY video_id
                   ) m ON m.video_id = v.id""",
                {"cid": client_id},
            ).fetchall()

        videos = [Video(**dict(row)) for row in rows]
        # unvoted 不是排序而是筛选: 只留还没被标记过的, 专门用来把数据填满
        if sort == "unvoted":
            videos = [v for v in videos if v.like + v.dislike == 0]
        self._sort(videos, sort)
        return videos

    @staticmethod
    def _sort(videos: list[Video], mode: str) -> None:
        """Wilson / 次数 / 时间 等排序都在这里定义。"""
        if mode == "wilson":
            # 评分降序; 同分时喜欢多的在前, 再按名字
            videos.sort(key=lambda v: (-v.wilson, -(v.like - v.dislike), natural_key(v.rel_path)))
        elif mode == "likes":
            videos.sort(key=lambda v: (-v.like, -v.wilson, natural_key(v.rel_path)))
        elif mode == "disliked":
            videos.sort(key=lambda v: (-v.dislike, v.wilson, natural_key(v.rel_path)))
        elif mode == "recent":
            # 从没标记过的排最前, 其次是最久没碰的 —— 用来持续发现新视频
            videos.sort(key=lambda v: (v.last_vote_at or 0.0, natural_key(v.rel_path)))
        elif mode == "unvoted":
            videos.sort(key=lambda v: natural_key(v.rel_path))
        else:  # "folder": 按目录浏览
            videos.sort(key=lambda v: natural_key(v.rel_path))

    def get(self, vid: str, client_id: str = "") -> Video | None:
        for v in self.list_videos("folder", client_id):
            if v.id == vid:
                return v
        return None

    # --------------------------------------------------------------- 写入

    def vote(self, vid: str, action: str, client_id: str) -> dict:
        """记录一次操作, 喜欢/不喜欢次数只增不减, 以便长期累积。"""
        if action not in ("like", "dislike"):
            raise ValueError(f"未知操作: {action}")
        with self.connect() as conn:
            self._require(conn, vid)
            conn.execute(
                "INSERT INTO votes (video_id, action, client_id, created_at) VALUES (?,?,?,?)",
                (vid, action, client_id, time.time()),
            )
        return {"applied": True, "undone": False, "video": self.get(vid, client_id).to_json()}

    def undo(self, vid: str, client_id: str) -> dict:
        """撤销本设备在该视频上的最近一次操作 (长按手势用)。"""
        with self.connect() as conn:
            self._require(conn, vid)
            last = conn.execute(
                """SELECT id FROM votes
                   WHERE video_id = ? AND client_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (vid, client_id),
            ).fetchone()
            if last is None:
                return {"applied": False, "undone": False,
                        "video": self.get(vid, client_id).to_json()}
            conn.execute("DELETE FROM votes WHERE id = ?", (last["id"],))
        return {"applied": False, "undone": True, "video": self.get(vid, client_id).to_json()}

    @staticmethod
    def _require(conn: sqlite3.Connection, vid: str) -> None:
        if conn.execute("SELECT 1 FROM videos WHERE id = ?", (vid,)).fetchone() is None:
            raise KeyError(vid)

    def stats(self, client_id: str = "") -> dict:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          COALESCE(SUM(t.action = 'like'), 0)    AS likes,
                          COALESCE(SUM(t.action = 'dislike'), 0) AS dislikes
                   FROM votes t JOIN videos v ON v.id = t.video_id"""
            ).fetchone()
            mine = conn.execute(
                """SELECT COALESCE(SUM(t.action = 'like'), 0)    AS likes,
                          COALESCE(SUM(t.action = 'dislike'), 0) AS dislikes
                   FROM votes t JOIN videos v ON v.id = t.video_id
                   WHERE t.client_id = ?""",
                (client_id,),
            ).fetchone()
            videos = conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]
            # 只数还存在的视频, 文件删掉后留下的孤儿记录不计入
            marked = conn.execute(
                """SELECT COUNT(DISTINCT t.video_id) AS n
                   FROM votes t JOIN videos v ON v.id = t.video_id"""
            ).fetchone()["n"]
        return {
            "videos": videos,
            "marked": marked,
            "unmarked": videos - marked,
            "likes": row["likes"],
            "dislikes": row["dislikes"],
            "my_likes": mine["likes"],
            "my_dislikes": mine["dislikes"],
        }
