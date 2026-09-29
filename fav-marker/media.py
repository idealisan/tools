"""媒体探测与 HLS 直播转码。

设计要点
--------
* 能被浏览器直接播放的文件 (mp4/h264 等) 走原文件, 不做任何转码。
* 播不了的 (mkv/avi/hevc/ac3/...) 交给 ffmpeg 边转边播, 输出 HLS 分片。
  分片只存在于一个临时目录, 客户端断开 / 会话超时后立刻删除 —— 不留缓存。
* 手机浏览器 (iOS Safari / Android Chrome) 原生支持 HLS, 直接喂 m3u8;
  桌面 Chrome / Firefox 用随项目附带的 hls.js。

这样同一个视频每次播放都重新转码, 磁盘占用恒定, 代价是 CPU。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

FFMPEG = os.environ.get("FAV_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FAV_FFPROBE", "ffprobe")

# 浏览器可直接播放的容器 + 编码组合
DIRECT_CONTAINERS = {".mp4", ".m4v", ".m4s", ".mov", ".webm"}
DIRECT_VCODECS = {"h264", "avc1", "vp8", "vp9", "av1"}
DIRECT_ACODECS = {"aac", "mp3", "opus", "vorbis", "none", ""}

# ffmpeg 输出 HLS 分片的间隔。服务端按这个把分片序号换算成时间
SEGMENT_SECONDS = 4

# 磁盘上保留多少分片。默认 100MB —— 720p 大约够 4~5 分钟,
# 剪枝时永远从"播放点再往前一点"开始删, 不会把正在看的分片删掉。
DEFAULT_CACHE_MB = 100
# 播放点之前额外多留这么多秒, 防止删到刚播过、还想往回拖的地方
PLAYHEAD_MARGIN_SECONDS = 30
MB = 1024 * 1024


def ffmpeg_available() -> bool:
    try:
        subprocess.run(
            [FFMPEG, "-version"], capture_output=True, timeout=10, check=True
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _pick_video_encoder(requested: str) -> str:
    """优先硬件编码, 退回 libx264。"""
    try:
        out = subprocess.run(
            [FFMPEG, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=20, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "libx264"
    if requested != "auto":
        return requested
    for enc, sysname in (("h264_videotoolbox", "darwin"), ("h264_nvenc", "linux")):
        if enc in out:
            return enc
    return "libx264"


@dataclass
class MediaInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    vcodec: str = ""
    acodec: str = ""
    container: str = ""
    direct: bool = False
    error: str = ""

    def to_json(self) -> dict:
        return {
            "duration": round(self.duration, 2),
            "width": self.width,
            "height": self.height,
            "vcodec": self.vcodec,
            "acodec": self.acodec,
            "container": self.container,
            "direct": self.direct,
            "error": self.error,
        }


def probe(path: str) -> MediaInfo:
    """用 ffprobe 判断能否直放, 并取时长/分辨率等元信息。"""
    ext = Path(path).suffix.lower()
    try:
        proc = subprocess.run(
            [FFPROBE, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return MediaInfo(container=ext, error=f"ffprobe 不可用: {exc}")
    if proc.returncode != 0:
        return MediaInfo(container=ext, error=proc.stderr.strip()[:200] or "ffprobe 失败")

    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        return MediaInfo(container=ext, error=f"ffprobe 输出无法解析: {exc}")

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    for candidate in (fmt.get("duration"), (video or {}).get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    info = MediaInfo(
        duration=duration,
        width=int((video or {}).get("width") or 0),
        height=int((video or {}).get("height") or 0),
        vcodec=str((video or {}).get("codec_name") or ""),
        acodec=str((audio or {}).get("codec_name") or "none"),
        container=ext,
    )
    if not video:
        info.error = "没有视频轨"
        return info
    if video.get("avg_frame_rate", "").startswith("0/") and duration == 0:
        info.error = "无法确定时长"

    info.direct = (
        ext in DIRECT_CONTAINERS
        and info.vcodec in DIRECT_VCODECS
        and info.acodec in DIRECT_ACODECS
    )
    return info


@dataclass
class Session:
    """一次「边转边播」的会话。临时分片目录在会话结束时删除。

    时间轴由本服务端说了算, 不交给 ffmpeg:
      base       —— 本地 0 秒对应的全局时间 (即 -ss 的值)
      playhead   —— 客户端最近取到第几片, 用来判断该往哪边剪
      pruned     —— 已经剪掉的最高分片序号
    """

    sid: str
    vid: str
    path: str
    info: MediaInfo
    tmpdir: str
    start: float = 0.0
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    proc: subprocess.Popen | None = None
    progress: float = 0.0
    state: str = "running"          # running | done | error
    cancelled: bool = False
    error: str = ""
    playhead: int = -1              # 客户端已取到的最高分片号
    produced: int = -1              # 已转出的最高分片号
    pruned: int = -1                # 已删除的最高分片号
    disk_bytes: int = 0
    throttled: bool = False         # 是否已把 ffmpeg 挂起 (背压)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def base(self) -> float:
        """本地时间轴 0 对应的全局时间。"""
        return self.start

    def global_of(self, index: int) -> float:
        return self.base + index * SEGMENT_SECONDS

    def where(self) -> dict:
        """当前可跳范围 (全局秒)。前端拿它决定是直接跳还是重开流。"""
        with self.lock:
            first = max(self.pruned + 1, 0)
            last = self.produced
            return {
                "base": round(self.base, 3),
                "available_start": round(self.global_of(first), 3),
                "available_end": round(self.global_of(last + 1), 3),
                "produced_end": round(self.global_of(last + 1), 3),
                "playhead": round(self.global_of(self.playhead), 3) if self.playhead >= 0 else None,
                "duration": round(self.info.duration, 3),
                "segments_on_disk": max(0, last - self.pruned),
                "pruned_hint": self.pruned >= 0,
                "disk_bytes": self.disk_bytes,
                "state": self.state,
                "throttled": self.throttled,
            }


def scan_segments(tmpdir: str) -> dict[int, str]:
    """tmpdir 里现有的分片: {序号: 完整路径}。"""
    out: dict[int, str] = {}
    try:
        names = os.listdir(tmpdir)
    except OSError:
        return out
    for name in names:
        if not name.startswith("seg") or not name.endswith(".ts"):
            continue
        try:
            out[int(name[3:8])] = os.path.join(tmpdir, name)
        except ValueError:
            continue
    return out


class HLSServer:
    """管理所有活跃转码会话。无预转码、无缓存。"""

    def __init__(
        self,
        tmp_root: str | None = None,
        encoder: str = "auto",
        crf: int = 23,
        max_height: int = 720,
        idle_timeout: int = 40,
        cache_mb: int = DEFAULT_CACHE_MB,
    ) -> None:
        self.tmp_root = tmp_root or os.environ.get("FAV_HLS_TMP") or tempfile.gettempdir()
        try:
            os.makedirs(self.tmp_root, exist_ok=True)
        except OSError as exc:
            print(f"转码临时目录 {self.tmp_root} 不可用 ({exc}), 改用系统临时目录", flush=True)
            self.tmp_root = tempfile.gettempdir()
        self.encoder = _pick_video_encoder(encoder)
        self.crf = crf
        self.max_height = max_height
        self.idle_timeout = idle_timeout
        self.cache_bytes = max(8, cache_mb) * MB
        self.cache_mb = max(8, cache_mb)
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self.available = ffmpeg_available()
        self._stop = threading.Event()
        self._janitor = threading.Thread(target=self._reap, daemon=True)
        self._janitor.start()
        atexit.register(self.shutdown)
        self._sweep_orphans()

    def _sweep_orphans(self) -> None:
        """清掉上次进程留下的分片目录。

        进程被强杀时 atexit 不会执行, 临时目录就会留在磁盘上。
        新的 HLSServer 意味着旧进程已经不在了, 只碰明显过期的那些,
        免得误伤同一台机器上另一个实例正在用的目录。
        """
        cutoff = time.time() - max(self.idle_timeout, 300)
        removed = 0
        try:
            candidates = list(Path(self.tmp_root).glob("favhls-*"))
        except OSError:
            return
        for path in candidates:
            try:
                if not path.is_dir() or path.stat().st_mtime > cutoff:
                    continue
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
            except OSError:
                continue
        if removed:
            print(f"清理上次残留的转码临时目录: {removed} 个", flush=True)

    # ------------------------------------------------------------ 对外接口

    def start(
        self,
        vid: str,
        path: str,
        start: float = 0.0,
        info: MediaInfo | None = None,
    ) -> tuple[Session | None, str]:
        """开启 (或复用) 一个转码会话, 返回 (session, error)。

        info 已经有 ffprobe 结果的话直接传进来, 省掉一次几百毫秒的外部调用。
        """
        if not self.available:
            return None, "未找到 ffmpeg, 无法转码"

        with self.lock:
            for s in self.sessions.values():
                if s.vid == vid and abs(s.start - start) < 0.5 and s.state == "running":
                    s.last_seen = time.time()
                    return s, ""

        if info is None:                      # 别在持锁的时候跑 ffprobe
            info = probe(path)

        with self.lock:
            for s in self.sessions.values():  # 上面探测期间可能已经有人开了同一个
                if s.vid == vid and abs(s.start - start) < 0.5 and s.state == "running":
                    s.last_seen = time.time()
                    return s, ""
            sid = uuid.uuid4().hex[:12]
            session = Session(
                sid=sid, vid=vid, path=path, info=info,
                tmpdir=tempfile.mkdtemp(prefix=f"favhls-{sid}-", dir=self.tmp_root),
                start=start,
            )
            self.sessions[sid] = session

        cmd = self._build_cmd(session)
        try:
            session.proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,   # 独立进程组, 方便整组回收
            )
        except OSError as exc:
            self._kill(session, f"ffmpeg 启动失败: {exc}")
            return None, f"ffmpeg 启动失败: {exc}"

        threading.Thread(target=self._pump, args=(session,), daemon=True).start()
        return session, ""

    def get(self, sid: str) -> Session | None:
        with self.lock:
            return self.sessions.get(sid)

    def count(self) -> int:
        """当前登记在册的会话数 (只数正在转的)。

        已转完的会话只是躺在那儿等播放器抓完最后几片, ffmpeg 已经退出,
        不占 CPU 也不该占名额。真正决定残留多久的是下面的存活判定。
        """
        with self.lock:
            return sum(1 for s in self.sessions.values() if s.state == "running")

    def mark_served(self, sid: str, index: int) -> None:
        """记下客户端取到哪一片 —— 播放点就靠它推算, 剪枝时不会误伤。"""
        s = self.get(sid)
        if s is None:
            return
        with s.lock:
            if index > s.playhead:
                s.playhead = index

    def refresh(self, s: Session) -> None:
        """重新数一遍分片, 顺带按上限剪枝 + 给 ffmpeg 施加背压。"""
        with s.lock:
            on_disk = scan_segments(s.tmpdir)
            if not on_disk:
                return
            sizes: dict[int, int] = {}
            total = 0
            for idx, path in on_disk.items():
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                sizes[idx] = size
                total += size
            s.disk_bytes = total
            s.produced = max(s.produced, max(on_disk))

            # 播放点至少要留够 margin 秒, 剪枝的起点永远在它前面
            floor = 0
            if s.playhead >= 0:
                floor = s.playhead - max(1, PLAYHEAD_MARGIN_SECONDS // SEGMENT_SECONDS)
            floor = max(floor, s.pruned)

            over = total - self.cache_bytes
            drop: list[int] = []
            for idx in sorted(on_disk):
                if idx < floor or idx <= s.pruned:
                    continue
                if over <= 0:
                    break
                drop.append(idx)
                over -= sizes[idx]
            for idx in drop:
                try:
                    os.remove(on_disk[idx])
                except OSError:
                    continue
                s.pruned = max(s.pruned, idx)
                s.disk_bytes -= sizes[idx]
            s.disk_bytes = max(0, s.disk_bytes)
        self._pace(s)

    def _pace(self, s: Session) -> None:
        """背压: 别让 ffmpeg 跑得太超前。

        转码比播放快好几倍, 不管的话几秒钟就能把整部片子转完、磁盘直接爆掉,
        而剪枝又不敢删播放点附近 (删了用户正在看的就没了)。
        所以超前太多就把进程挂起, 播放追上来再放行 —— 磁盘占用和
        "播放点前面留多少" 就绑死在 --hls-cache-mb 上了。

        上限是**每个会话各一份**: 同时在看几个片子, 磁盘就占几倍。
        真实用法基本只有 1~2 个, 想更省就调小 --hls-cache-mb。
        """
        proc = s.proc
        if proc is None or proc.poll() is not None or s.cancelled:
            return
        share = self.cache_bytes
        with s.lock:
            if s.produced < 0:
                return
            # 没人取过分片说明还没开始看, 按播放点在 0 处理。
            # 不然这种"开了就扔下"的会话会一路转完, 把磁盘撑爆。
            playhead = max(s.playhead, 0)
            count = s.produced - max(s.pruned, -1)
            if count < 3 or s.disk_bytes <= 0:
                return
            avg = s.disk_bytes / count
            # 留多少超前: 磁盘上限减去回看余量, 换算成分片数
            budget = max(4, int(share * 0.85) // max(int(avg), 1))
            ahead_segments = s.produced - playhead
            should_pause = ahead_segments >= budget
            if should_pause == s.throttled:
                return
            s.throttled = should_pause
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGSTOP if should_pause else signal.SIGCONT)
        except (OSError, ProcessLookupError):
            with s.lock:
                s.throttled = False

    def touch(self, sid: str) -> None:
        with self.lock:
            s = self.sessions.get(sid)
            if s is not None:
                s.last_seen = time.time()

    def where(self, sid: str) -> dict | None:
        """当前会话能跳到哪里。前端拖完进度条先问这个再决定跳不跳。"""
        s = self.get(sid)
        if s is None:
            return None
        self.refresh(s)
        return s.where()

    def stop(self, sid: str) -> None:
        with self.lock:
            session = self.sessions.pop(sid, None)
        if session is not None:
            self._kill(session, "客户端已断开")

    def shutdown(self) -> None:
        self._stop.set()
        with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for s in sessions:
            self._kill(s, "服务关闭")

    def status_json(self) -> dict:
        with self.lock:
            items = list(self.sessions.values())
        return {
            "ffmpeg": self.available,
            "encoder": self.encoder,
            "crf": self.crf,
            "max_height": self.max_height,
            "segment_seconds": SEGMENT_SECONDS,
            "cache_mb": self.cache_mb,
            "idle_timeout": self.idle_timeout,
            "active": [
                {"sid": s.sid, "video": s.vid, "state": s.state,
                 "progress": round(s.progress, 3), "error": s.error,
                 "disk_bytes": s.disk_bytes, "throttled": s.throttled,
                 "segments": max(0, s.produced - s.pruned),
                 "playhead": s.playhead, "tmpdir": s.tmpdir}
                for s in items
            ],
        }

    # -------------------------------------------------------------- 内部

    def _build_cmd(self, s: Session) -> list[str]:
        cmd = [
            FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-fflags", "+genpts",
        ]
        if s.start > 0.05:
            cmd += ["-ss", f"{s.start:.3f}"]
        cmd += [
            "-i", s.path,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-sn", "-dn",
            "-vf", rf"scale=-2:min({self.max_height}\,ih)",
            "-c:v", self.encoder,
        ]
        if self.encoder == "libx264":
            cmd += ["-preset", "veryfast", "-crf", str(self.crf)]
        else:
            cmd += ["-b:v", "2500k"]
        cmd += [
            "-pix_fmt", "yuv420p",
            "-g", str(SEGMENT_SECONDS * 12),   # 关键帧密度, 让分片边界对齐
            "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-f", "hls",
            "-hls_time", str(SEGMENT_SECONDS),
            # 分片一律不删: 让服务端按字节上限剪, 而且永远从播放点前面删。
            # ffmpeg 自己的 delete_segments 删的是"最新 N 片", 而转码跑在
            # 播放点前面, 那等于把用户正在看的分片从磁盘上抹掉。
            "-hls_list_size", "0",
            "-hls_flags", "temp_file+independent_segments+append_list",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", os.path.join(s.tmpdir, "seg%05d.ts"),
            # HLS 产物写文件, stdout 空着用来读进度
            "-progress", "pipe:1", "-nostats",
            os.path.join(s.tmpdir, "index.m3u8"),
        ]
        return cmd

    def _pump(self, s: Session) -> None:
        """读 ffmpeg 的 -progress 输出, 更新进度。"""
        proc = s.proc
        assert proc is not None
        duration = max(s.info.duration - s.start, 0.001)
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("out_time_ms="):
                    continue
                try:
                    micros = float(line.split("=", 1)[1])
                except ValueError:
                    continue
                s.progress = max(0.0, min(1.0, (micros / 1_000_000.0) / duration))
            proc.wait(timeout=10)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        finally:
            code = proc.poll()
            if s.state == "running" and not s.cancelled:
                if code == 0 or self._playlist_exists(s):
                    s.state = "done"
                    s.progress = 1.0
                else:
                    s.state = "error"
                    s.error = self._read_stderr(proc)
            s.last_seen = time.time()
            # 不再"转完就固定留 30 秒" —— 那种做法有两个问题:
            #   一是白占 30 秒, 二是客户端一旦离开就没必要留着;
            # 改由存活判定统一处理 (见 _reap), 还在取分片的会话不会被动到。
            if s.cancelled:
                self._cleanup(s)

    @staticmethod
    def _read_stderr(proc: subprocess.Popen) -> str:
        try:
            if proc.stderr is None:
                return "转码失败"
            return proc.stderr.read().decode("utf-8", "replace").strip()[-400:] or "转码失败"
        except (OSError, ValueError):
            return "转码失败"

    @staticmethod
    def _playlist_exists(s: Session) -> bool:
        return os.path.isfile(os.path.join(s.tmpdir, "index.m3u8"))

    def _kill(self, s: Session, reason: str) -> None:
        s.cancelled = True
        proc = s.proc
        if proc is not None and s.throttled:
            # 挂起中的进程收不到 SIGTERM, 得先放行
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGCONT)
            except (OSError, ProcessLookupError):
                pass
            s.throttled = False
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    proc.kill()
        self._cleanup(s, reason)

    def _cleanup(self, s: Session, reason: str = "") -> None:
        with s.lock:
            shutil.rmtree(s.tmpdir, ignore_errors=True)
        with self.lock:
            # 分片已经没了, 这个会话就没有存在价值了, 无论之前是什么状态
            if self.sessions.get(s.sid) is s:
                self.sessions.pop(s.sid, None)

    def _reap(self) -> None:
        """回收客户端不再理会的会话。

        不用"转完就固定留 N 秒"那种做法 —— 那种判据两边都会错:
        客户端可能还在取最后几片 (提前收掉就播不了), 也可能早就走了
        (硬留 30 秒就是残留)。这里只看"还有没有人理我":
        播放列表、分片、心跳, 任何一次请求都会刷新 last_seen。
        """
        while not self._stop.wait(2.0):
            now = time.time()
            with self.lock:
                idle = [s for s in self.sessions.values()
                        if now - s.last_seen > self.idle_timeout]
                for s in idle:
                    self.sessions.pop(s.sid, None)
            for s in idle:
                self._kill(s, "客户端不再请求")
            # 还在的会话要保持背压, 否则没在取分片的话会一路转完撑爆磁盘
            with self.lock:
                alive = list(self.sessions.values())
            for s in alive:
                if s.state == "running":
                    self.refresh(s)
