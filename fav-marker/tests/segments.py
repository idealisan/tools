"""服务端分片管理验收: 不再被 ffmpeg 删 / 100MB 上限 / 不误伤播放点。"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('PORT', '5099')}"
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.load(r)


def post(path: str, payload: dict):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Client-Id": "srv-test"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def playlist(sid: str) -> str:
    with urllib.request.urlopen(f"{BASE}/hls/{sid}/index.m3u8", timeout=60) as r:
        return r.read().decode()


def main() -> int:
    lib = get("/api/library?sort=folder")
    # 挑最长的那个待转码视频, 够长才能看出剪枝
    cand = [v for v in lib["videos"] if not v.get("direct")]
    if not cand:
        print("  没有需要转码的视频, 跳过")
        return 0
    target = max(cand, key=lambda v: v.get("duration") or 0)
    print(f"\n  用 {target['title']} (约 {target.get('duration')} 秒) 做验证")
    cap_mb = get("/api/media/status").get("cache_mb", 100)
    cap = cap_mb * 1024 * 1024
    print(f"  磁盘上限 {cap_mb} MB")

    status, info = post("/api/hls/start", {"id": target["id"]})
    check("转码会话已建立", status == 200, f"HTTP {status}")
    sid = info["sid"]
    check("start 返回 base", info.get("base") == 0.0, f"base={info.get('base')}")

    # 等它转够一会儿, 应该超过 100MB 上限
    print("  等转码产出一部分…")
    peak = 0
    for _ in range(200):
        try:
            w = get(f"/api/hls/where?sid={sid}")
        except urllib.error.HTTPError:
            break
        peak = max(peak, w["disk_bytes"])
        if w["segments_on_disk"] > 4 and w["pruned_hint"]:
            break
        time.sleep(0.5)
    w = get(f"/api/hls/where?sid={sid}")
    if not w.get("pruned_hint"):
        print(f"  注意: 整段转完也没撑到 {cap_mb}MB 上限, 剪枝分支没被触发。")
        print("        剪枝要用小上限才测得到, 例如:")
        print("        SERVER_ARGS='--hls-cache-mb 8' ./tests/with_server.sh tests/segments.py")
        post("/api/hls/stop", {"sid": sid})
        return 0
    mb = peak / 1024 / 1024
    check("磁盘占用没超上限", 0 < peak <= cap * 1.2, f"峰值 {mb:.1f} MB (上限 {cap_mb})")
    check("确实转出了内容", w["produced_end"] > 0, f"已转到 {w['produced_end']:.0f}s")

    w = get(f"/api/hls/where?sid={sid}")
    check("确实触发了剪枝", w["segments_on_disk"] < w["produced_end"] / 4,
          f"转出到 {w['produced_end']:.0f}s, 盘上只剩 {w['segments_on_disk']} 片")
    pl = playlist(sid)
    segs = [ln for ln in pl.splitlines() if ln.startswith("seg")]
    first = int(segs[0][3:8])
    last = int(segs[-1][3:8])
    check("播放列表从第 0 片开始 (时间轴不平移)", first == 0, f"第一片 seg{first:05d}")
    check("播放列表列出了全部已转出的片", last >= w["segments_on_disk"] - 1,
          f"列表到 seg{last:05d}, 磁盘 {w['segments_on_disk']} 片")
    check("剪掉的片仍在列表里 (保证时间轴连续)", len(segs) > w["segments_on_disk"],
          f"列表 {len(segs)} 条 > 磁盘 {w['segments_on_disk']} 片")

    # 取几片, 模拟客户端在播
    print("  模拟客户端取片…")
    got = 0
    for i in range(w["segments_on_disk"]):
        idx = first + i
        try:
            with urllib.request.urlopen(f"{BASE}/hls/{sid}/seg{idx:05d}.ts", timeout=30) as r:
                if r.status == 200:
                    got += 1
        except urllib.error.HTTPError:
            pass
    check("能取到分片", got > 0, f"{got} 片")
    w2 = get(f"/api/hls/where?sid={sid}")
    check("服务端记住了播放点", w2["playhead"] is not None,
          f"playhead={w2['playhead']} (全局秒)")

    # 剪枝不能把播放点附近剪掉
    if w2["playhead"] is not None:
        margin = w2["playhead"] - w2["available_start"]
        check("播放点还在可跳范围内", margin >= 0,
              f"播放点 {w2['playhead']:.0f}s, 可跳起点 {w2['available_start']:.0f}s")

    # 已剪掉的片应该 404
    if w2["segments_on_disk"] < len(segs):
        old = first
        code = 0
        try:
            urllib.request.urlopen(f"{BASE}/hls/{sid}/seg{old:05d}.ts", timeout=10)
        except urllib.error.HTTPError as e:
            code = e.code
        check("被剪掉的片返回 404", code == 404, f"HTTP {code}")

    post("/api/hls/stop", {"sid": sid})
    print("\n" + "=" * 46)
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
