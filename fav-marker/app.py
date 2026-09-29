"""视频收藏 Web 播放器 —— 后端入口。

用法:
    python app.py                     # 扫描当前目录
    python app.py --videos ~/Movies   # 扫描指定目录 (可多个)
    python app.py --port 5000
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import signal
import socket
import time
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file

import media
from library import Library

__version__ = "0.1.0"

SORTS = ("folder", "wilson", "likes", "disliked", "recent", "unvoted")

# 路由里的 ".ts" 后缀会被 Flask 剥掉, 这里只校验剩下的文件名
SEGMENT_NAME = re.compile(r"^seg\d{5}$")

# 自己指定, 不依赖系统 mimetypes 数据库 (不同机器差别很大)
MIME_BY_EXT = {
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".webm": "video/webm",
    ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo", ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv",
    ".asf": "video/x-ms-asf", ".divx": "video/divx", ".f4v": "video/x-f4v",
    ".ts": "video/MP2T", ".m2ts": "video/MP2T", ".mts": "video/MP2T",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".3gp": "video/3gpp", ".ogv": "video/ogg", ".vob": "video/x-vob",
    ".rmvb": "application/vnd.rn-realmedia-vbr", ".rm": "application/vnd.rn-realmedia",
    ".rmm": "application/vnd.rn-realmedia",
}

# 同一时间最多开几个转码。
# 留 2 而不是 1: 手机上刷新页面时不会发 /api/hls/stop, 旧会话还在转,
# 正好卡住 1 的话新会话必被拒。2 既能扛住刷新重开, 也能两台设备各看各的。
# 磁盘上限是所有会话共享的 (见 media.HLSServer._pace), 所以不会翻倍。
MAX_HLS_SESSIONS = 2


def client_id() -> str:
    return request.headers.get("X-Client-Id", "")[:64]


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def create_app(roots: list[str], db_path: str, hls: media.HLSServer) -> Flask:
    app = Flask(__name__)
    lib = Library(roots, db_path)

    stats = lib.scan()
    print(
        f"扫描完成: {stats['total']} 个视频 "
        f"(新增 {stats['added']}, 移除 {stats['removed']})",
        flush=True,
    )
    for root in lib.roots:
        print(f"  · {root}", flush=True)
    if hls.available:
        print(f"ffmpeg 转码: {hls.encoder}, 最高 {hls.max_height}p, 直播式 HLS (不留缓存)",
              flush=True)
    else:
        print("⚠ 未找到 ffmpeg, mkv/avi/hevc 等格式将无法播放", flush=True)

    @app.after_request
    def no_cache(resp: Response) -> Response:
        if request.path.startswith(("/media", "/api", "/hls")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------- 页面

    @app.get("/")
    def index():
        return render_template("index.html", defaults={"sort": "folder", "autoplay": False})

    # ------------------------------------------------------------- 视频流

    @app.get("/media/<vid>")
    def media_file(vid: str):
        path = lib.path_of(vid)
        if path is None or not os.path.isfile(path):
            abort(404)
        ext = Path(path).suffix.lower()
        mimetype = MIME_BY_EXT.get(ext) or mimetypes.guess_type(path)[0] or "video/mp4"
        # conditional=True 让 Werkzeug 处理 Range / If-Range, 手机才能拖进度条
        return send_file(path, mimetype=mimetype, conditional=True)

    # ------------------------------------------------------------- 直播 HLS

    @app.get("/hls/<sid>/index.m3u8")
    def hls_playlist(sid: str):
        s = hls.get(sid)
        if s is None:
            abort(404)
        hls.touch(sid)
        hls.refresh(s)

        # 边转边播时, 播放器几乎总是在第一个分片出现之前就来要播放列表。
        # 短暂等一下, 还没有就回 404 —— 播放器会按直播流的标准做法重试,
        # 直接给一个空的 #EXTM3U 反而会让 hls.js 解析失败。
        deadline = time.monotonic() + 3.0
        while True:
            text = _read_text(os.path.join(s.tmpdir, "index.m3u8"))
            if text and "#EXTINF" in text:
                break
            if s.state != "running" or time.monotonic() >= deadline:
                abort(404)
            time.sleep(0.2)

        # ffmpeg 正在追加时最后一行可能是半截的, 丢掉不完整的行。
        # 已剪掉的分片仍然留在列表里 —— 删条目会让播放器的整个时间轴平移。
        lines = [ln for ln in text.splitlines() if ln.startswith(("#", "seg"))]
        if s.base > 0.05:
            # 告诉播放器这条流在片子里是从哪儿开始的 (HLS 标准字段),
            # iOS 原生播放器靠它把 currentTime 对回真实时间
            lines.insert(1, f"#EXT-X-START:TIME-OFFSET={s.base:.3f}")
        return Response("\n".join(lines) + "\n",
                        mimetype="application/vnd.apple.mpegurl")

    @app.get("/hls/<sid>/<name>.ts")
    def hls_segment(sid: str, name: str):
        s = hls.get(sid)
        if s is None or not SEGMENT_NAME.match(name):
            abort(404)
        hls.touch(sid)
        hls.refresh(s)
        path = os.path.join(s.tmpdir, f"{name}.ts")   # 路由里 ".ts" 被剥掉了, 要补回
        if not os.path.isfile(path):
            abort(404)                                  # 已被剪掉
        hls.mark_served(sid, int(name[3:8]))
        return send_file(path, mimetype="video/MP2T", conditional=True)

    def _cached_probe(vid: str) -> media.MediaInfo | None:
        """库里存着的 ffprobe 结果, 存过就直接复用, 不用再跑一次外部命令。"""
        cached = lib.media_info(vid)
        if cached is None:
            return None
        return media.MediaInfo(
            duration=cached["duration"] or 0.0,
            width=cached["width"] or 0,
            height=cached["height"] or 0,
            vcodec=cached["vcodec"] or "",
            acodec=cached["acodec"] or "none",
            direct=bool(cached["direct"]),
        )

    @app.post("/api/hls/start")
    def api_hls_start():
        body = request.get_json(silent=True) or {}
        vid = str(body.get("id", ""))
        try:
            start = max(0.0, float(body.get("start", 0)))
        except (TypeError, ValueError):
            start = 0.0
        path = lib.path_of(vid)
        if path is None or not os.path.isfile(path):
            return jsonify({"error": "视频不存在"}), 404
        # 先把转完的旧会话收掉: 手机上"看完一个马上点下一个"很常见,
        # 不先收的话旧会话占着名额, 新会话直接被判成 503
        hls.reap_finished()
        if hls.count() >= MAX_HLS_SESSIONS:
            return jsonify({"error": "转码任务太多, 等一会儿再试"}), 503
        session, err = hls.start(vid, path, start, info=_cached_probe(vid))
        if session is None:
            return jsonify({"error": err or "无法启动转码"}), 503
        return jsonify(
            {
                "sid": session.sid,
                "playlist": f"/hls/{session.sid}/index.m3u8",
                "duration": session.info.duration,
                "start": start,
                # 本地时间轴 0 对应的全局时间, 前端靠它把进度条对回真实位置
                "base": round(session.base, 3),
                "encoder": hls.encoder,
                "where": f"/api/hls/where?sid={session.sid}",
            }
        )

    @app.get("/api/hls/where")
    def api_hls_where():
        sid = request.args.get("sid", "")
        info = hls.where(sid)
        if info is None:
            return jsonify({"error": "会话不存在"}), 404
        hls.touch(sid)
        return jsonify(info)

    @app.post("/api/hls/stop")
    def api_hls_stop():
        body = request.get_json(silent=True) or {}
        hls.stop(str(body.get("sid", "")))
        return jsonify({"ok": True})

    # --------------------------------------------------------------- API

    @app.get("/api/library")
    def api_library():
        sort = request.args.get("sort", "folder")
        if sort not in SORTS:
            sort = "folder"
        videos = lib.list_videos(sort, client_id())
        return jsonify(
            {
                "sort": sort,
                "total": len(videos),
                "ffmpeg": hls.available,        # 前端要靠它决定能不能转码
                "stats": lib.stats(client_id()),
                "videos": [v.to_json() for v in videos],
            }
        )

    @app.get("/api/playback/<vid>")
    def api_playback(vid: str):
        """告诉前端这个视频直放还是转 HLS, 以及时长。"""
        path = lib.path_of(vid)
        if path is None or not os.path.isfile(path):
            return jsonify({"error": "视频不存在"}), 404
        cached = lib.media_info(vid)
        if cached is not None:
            direct = bool(cached["direct"])
            duration = cached["duration"] or 0.0
            detail = {k: cached[k] for k in ("width", "height", "vcodec", "acodec")}
        else:
            info = media.probe(path)
            lib.save_media_info(vid, info)
            direct = info.direct
            duration = info.duration
            detail = {"width": info.width, "height": info.height,
                      "vcodec": info.vcodec, "acodec": info.acodec}
            if info.error:
                detail["error"] = info.error
        return jsonify(
            {
                "id": vid,
                "direct": direct,
                "duration": round(duration or 0.0, 2),
                "ffmpeg": hls.available,
                **detail,
            }
        )

    def _vote_response(fn):
        """vote 和 undo 共用的收尾: 校验 client、翻译异常。"""
        body = request.get_json(silent=True) or {}
        vid = str(body.get("id", ""))
        action = str(body.get("action", ""))
        cid = client_id()
        if not cid:
            return jsonify({"error": "缺少 X-Client-Id"}), 400
        try:
            return jsonify(fn(vid, action, cid))
        except KeyError:
            return jsonify({"error": "视频不存在"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/vote")
    def api_vote():
        return _vote_response(lib.vote)

    @app.post("/api/undo")
    def api_undo():
        return _vote_response(lambda vid, act, cid: lib.undo(vid, cid))

    @app.post("/api/rescan")
    def api_rescan():
        return jsonify(lib.scan())

    @app.get("/api/stats")
    def api_stats():
        return jsonify(lib.stats(client_id()))

    @app.get("/api/media/status")
    def api_media_status():
        return jsonify({**hls.status_json(), "max_sessions": MAX_HLS_SESSIONS})

    @app.get("/api/health")
    def api_health():
        return jsonify({"ok": True, "version": __version__,
                        "videos": lib.stats()["videos"], "ffmpeg": hls.available})

    return app


def local_ip() -> str:
    """取本机在局域网里的地址, 手机要连这个。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def resolve(path: str) -> str:
    """展开 ~ 和相对路径, 顺带把软链接解掉, 保证同一个文件不会被扫成两条。"""
    return os.path.realpath(os.path.expanduser(str(path)))


def load_config() -> dict:
    path = Path("config.json")
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"config.json 解析失败, 已忽略: {exc}", flush=True)
            return {}
        if not isinstance(data, dict):
            print("config.json 顶层必须是对象, 已忽略", flush=True)
            return {}
        return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


def main() -> None:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="视频收藏 Web 播放器")
    parser.add_argument("--videos", nargs="+", help="视频目录, 默认当前目录")
    parser.add_argument("--host", default="0.0.0.0", help="默认 0.0.0.0, 允许手机访问")
    parser.add_argument("--port", type=int, default=cfg.get("port", 5000))
    parser.add_argument("--db", default=cfg.get("db", "data/fav.db"))
    parser.add_argument("--encoder", default=cfg.get("encoder", "auto"),
                        help="转码编码器: auto / libx264 / h264_videotoolbox / h264_nvenc")
    parser.add_argument("--crf", type=int, default=cfg.get("crf", 23),
                        help="libx264 质量, 越小越清晰越慢 (默认 23)")
    parser.add_argument("--max-height", type=int, default=cfg.get("max_height", 720),
                        help="转码输出最高高度, 手机上 720 足够 (默认 720)")
    parser.add_argument("--hls-cache-mb", type=int, default=cfg.get("hls_cache_mb", media.DEFAULT_CACHE_MB),
                        help="转码分片在磁盘上最多留多少 MB (默认 100)")
    args = parser.parse_args()

    roots = [resolve(p) for p in (args.videos or cfg.get("video_roots") or ["."])]
    db_path = resolve(args.db)

    hls = media.HLSServer(encoder=args.encoder, crf=args.crf, max_height=args.max_height,
                          cache_mb=args.hls_cache_mb)
    app = create_app(roots, db_path, hls)
    print(f"\n  v{__version__}   手机浏览器打开:  http://{local_ip()}:{args.port}\n", flush=True)

    # Ctrl-C / kill 时 Python 默认直接退出, atexit 不会跑, 转码就变成孤儿了
    def _bye(_signum, _frame):
        hls.shutdown()
        raise SystemExit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _bye)
        except (ValueError, OSError):
            pass

    try:
        app.run(host=args.host, port=args.port, threaded=True, debug=False)
    finally:
        hls.shutdown()


if __name__ == "__main__":
    main()
