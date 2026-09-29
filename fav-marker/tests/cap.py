"""100MB 上限实测: 拿 10 分钟的片子真播一段, 看临时目录会不会失控。"""

import json
import os
import subprocess
import time
import urllib.request

from playwright.sync_api import sync_playwright

BASE = f"http://127.0.0.1:{os.environ.get('PORT', '5099')}"
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def api(path: str):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.load(r)


def dir_bytes(path: str) -> int:
    """只量当前会话那一个目录, 免得被别的测试残留干扰。"""
    try:
        return sum(os.path.getsize(os.path.join(path, f))
                   for f in os.listdir(path)
                   if f.startswith("seg") and f.endswith(".ts"))
    except OSError:
        return 0


def main() -> int:
    cap_mb = api("/api/media/status").get("cache_mb", 100)
    print(f"\n  上限 {cap_mb} MB")

    lib = api("/api/library?sort=folder")
    for v in lib["videos"]:
        if v.get("direct") is None:
            try:
                api(f"/api/playback/{v['id']}")
            except Exception:
                pass
    lib = api("/api/library?sort=folder")
    cand = [v for v in lib["videos"] if v.get("direct") is False and (v.get("duration") or 0) > 300]
    if not cand:
        print("  没有 5 分钟以上的待转码视频, 跳过")
        return 0
    target = max(cand, key=lambda v: v["duration"])
    print(f"  目标: {target['title']} ({target['duration']:.0f} 秒)")

    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome",
                              args=["--autoplay-policy=no-user-gesture-required"])
        ctx = b.new_context(viewport={"width": 390, "height": 844},
                            is_mobile=True, has_touch=True)
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_function('() => document.querySelector("#player").readyState >= 1',
                               timeout=60000)
        page.locator("#hintClose").click()
        page.wait_for_timeout(300)
        page.locator("#sortBtn").click()
        page.locator("#sortMenu button", has_text="按文件名").click()
        page.wait_for_timeout(800)
        box = page.locator("#stage").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        for _ in range(12):
            if page.locator("#title").inner_text() == target["title"]:
                break
            page.mouse.move(cx, cy)
            page.mouse.down()
            for k in range(1, 13):
                page.mouse.move(cx, cy - 220 * k / 12)
                page.wait_for_timeout(8)
            page.mouse.up()
            page.wait_for_timeout(800)
        check("定位到长视频", page.locator("#title").inner_text() == target["title"],
              page.locator("#title").inner_text())

        # 让转码狂跑一阵, 远超 100MB
        print("  让转码全速跑 40 秒 (远超过上限)...")
        page.wait_for_function('() => document.querySelector("#player").currentTime > 0',
                               timeout=180000)
        st = api("/api/media/status")
        tmpdir = st["active"][0]["tmpdir"] if st["active"] else ""
        peak = 0
        disk_peak = 0
        saw_throttle = False
        for _ in range(20):
            time.sleep(2)
            peak = max(peak, dir_bytes(tmpdir))
            st = api("/api/media/status")
            if st["active"]:
                w = st["active"][0]
                disk_peak = max(disk_peak, w.get("disk_bytes", 0))
                saw_throttle = saw_throttle or w.get("throttled", False)
        w = st["active"][0] if st["active"] else {}
        print(f"    转码进度 {w.get('progress', 0):.0%}  已挂起={w.get('throttled')}  "
              f"服务端记账峰值 {disk_peak/1024/1024:.1f}MB  du 峰值 {peak/1024/1024:.1f}MB")
        check("确实用过背压 (ffmpeg 被挂起过)", saw_throttle or w.get("progress", 1) < 0.95,
              f"throttled={w.get('throttled')}")
        check("服务端记账没超上限", 0 < disk_peak <= cap_mb * 1024 * 1024,
              f"峰值 {disk_peak/1024/1024:.1f} MB")
        check("磁盘上真实占用没超上限", 0 < peak <= cap_mb * 1024 * 1024 * 1.02,
              f"峰值 {peak/1024/1024:.1f} MB")
        check("播放没被打断", not errs, "; ".join(errs[:2]))
        pct = float(page.evaluate(
            '() => parseFloat(document.querySelector("#bar-fill").style.width) || 0'))
        check("进度条正常推进", pct > 0, f"{pct:.1f}%")
        b.close()

    print("\n" + "=" * 46)
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
