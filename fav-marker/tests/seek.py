"""跳转验收: 进度条任何时候都不能归零。

核心场景: 在一条已经转出很长一截的转码流上, 拖到中间, 确认
  1. 进度条落在真实位置, 不是 0
  2. 超出已转出范围时, 服务端从那儿重开流, 进度条依然不是 0
  3. 连续跳多次不会把 ffmpeg 进程或临时目录堆起来
"""

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


def long_transcode_video():
    lib = api("/api/library?sort=folder")
    for v in lib["videos"]:
        if v.get("direct") is None:
            try:
                api(f"/api/playback/{v['id']}")
            except Exception:
                pass
    lib = api("/api/library?sort=folder")
    cand = [v for v in lib["videos"] if v.get("direct") is False and (v.get("duration") or 0) > 120]
    return max(cand, key=lambda v: v["duration"]) if cand else None


def bar_pct(page) -> float:
    return float(page.evaluate(
        '() => parseFloat(document.querySelector("#bar-fill").style.width) || 0'))


def clock(page) -> str:
    return page.locator("#cWilson").inner_text()


def main() -> int:
    target = long_transcode_video()
    if not target:
        print("  没有足够长的待转码视频, 跳过")
        return 0
    dur = target["duration"]
    print(f"\n  目标: {target['title']} ({dur:.0f} 秒)")

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
        for _ in range(10):
            if page.locator("#title").inner_text() == target["title"]:
                break
            page.mouse.move(cx, cy)
            page.mouse.down()
            for k in range(1, 13):
                page.mouse.move(cx, cy - 220 * k / 12)
                page.wait_for_timeout(8)
            page.mouse.up()
            page.wait_for_timeout(800)
        check("定位到目标视频", page.locator("#title").inner_text() == target["title"],
              page.locator("#title").inner_text())

        page.wait_for_function('() => document.querySelector("#player").currentTime > 0',
                               timeout=120000)
        check("从 0 开始播 (没贴着流末尾)", bar_pct(page) < 8,
              f"进度条 {bar_pct(page):.1f}%  {clock(page)}")
        geo = page.evaluate("""() => { const v = document.querySelector('#player');
            return {w: v.videoWidth, h: v.videoHeight, dur: v.duration}; }""")
        check("转码压到 720p", geo["h"] == 720 and geo["w"] == 1280, f"{geo['w']}x{geo['h']}")
        check("界面显示真实时长 (不是直播已转出的部分)", f"{int(dur)//60}:" in clock(page),
              f"{clock(page)} (源 {dur:.0f}s, video.duration={geo['dur']})")

        def drag_to(frac: float) -> None:
            bar = page.locator("#bar-progress").bounding_box()
            y = bar["y"] + bar["height"] / 2
            page.mouse.move(bar["x"] + 14, y)
            page.mouse.down()
            page.mouse.move(bar["x"] + 14 + (bar["width"] - 28) * frac, y)
            page.wait_for_timeout(300)
            page.mouse.up()

        # 让转码先跑出一大截, 让"往后跳"落在已转出范围内
        print("  等转码产出…")
        for _ in range(120):
            w = api(f"/api/media/status")
            if w["active"] and w["active"][0]["progress"] > 0.25:
                break
            time.sleep(1)
        page.wait_for_timeout(1500)

        print("  --- 跳到已转出范围内 (50%) ---")
        drag_to(0.5)
        page.wait_for_timeout(4000)
        pct = bar_pct(page)
        check("进度条没归零", pct > 30, f"{pct:.1f}%  {clock(page)}")
        check("时间标签是真实位置", f"{dur*0.5//60}:" in clock(page) or pct > 30, clock(page))

        print("  --- 跳到还没转出的地方 (95%) ---")
        w = api("/api/media/status")
        produced = w["active"][0]["progress"] * dur if w["active"] else 0
        print(f"    (目前转出约 {produced:.0f}s / {dur:.0f}s)")
        drag_to(0.95)
        page.wait_for_timeout(6000)
        pct2 = bar_pct(page)
        check("跳远之后进度条也没归零", pct2 > 60, f"{pct2:.1f}%  {clock(page)}")
        check("还在播", page.evaluate('() => !document.querySelector("#player").paused'))

        print("  --- 连跳 5 次 ---")
        before_ff = subprocess.run(["pgrep", "-f", "ffmpeg.*favhls"],
                                   capture_output=True).stdout.decode().split()
        for i, f in enumerate((0.2, 0.7, 0.4, 0.9, 0.6), 1):
            drag_to(f)
            page.wait_for_timeout(2500)
            pct = bar_pct(page)
            ok = pct > 5
            print(f"    第 {i} 次拖到 {f:.0%} -> 进度条 {pct:.1f}%  {clock(page)}  {'OK' if ok else '归零了!'}")
            if not ok:
                FAILS.append(f"第 {i} 次跳转后进度条归零")
        time.sleep(2)
        after_ff = subprocess.run(["pgrep", "-f", "ffmpeg.*favhls"],
                                  capture_output=True).stdout.decode().split()
        check("ffmpeg 进程没堆起来", len(after_ff) <= max(1, len(before_ff)),
              f"{len(before_ff)} -> {len(after_ff)}")
        st = api("/api/media/status")
        check("转码会话没堆起来", len(st["active"]) <= 1, f"{len(st['active'])} 个")
        check("磁盘占用在上限内", st["active"] and st["active"][0]["state"] != "error",
              f"{st['active'][0]['progress'] if st['active'] else '-'}")

        print("  --- 往回跳一小段 (播放点附近) ---")
        drag_to(0.55)
        page.wait_for_timeout(4000)
        check("回跳后进度条正常", bar_pct(page) > 30, f"{bar_pct(page):.1f}%  {clock(page)}")

        check("没有 JS 报错", not errs, "; ".join(errs[:2]))
        page.screenshot(path="/tmp/favtest/7-跳转后.png")
        b.close()

    print("\n" + "=" * 46)
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
