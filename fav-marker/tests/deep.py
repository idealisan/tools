"""单片深测: 盯住一个视频在两条播放路径下的表现。

    python tests/deep.py <视频名关键字>

分别模拟:
  - hls.js  (Android Chrome / 桌面)
  - 原生 HLS (iPhone Safari)
直播过程中播放器随时可能停在第一片上, 这里全程盯 readyState / currentTime /
buffered / error, 超过阈值没推进就判定为卡住。
"""

import json
import os
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

BASE = f"http://127.0.0.1:{os.environ.get('PORT', '5099')}"
FAILS: list[str] = []

IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
ANDROID_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")


def api(path: str):
    with urllib.request.urlopen(BASE + path, timeout=180) as r:
        return json.load(r)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"    {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def probe(page) -> dict:
    return page.evaluate("""() => { const v = document.querySelector('#player');
        return {rs: v.readyState, ns: v.networkState, paused: v.paused, ended: v.ended,
                t: +v.currentTime.toFixed(2), dur: v.duration,
                buf: v.buffered.length ? +v.buffered.end(v.buffered.length-1).toFixed(1) : 0,
                err: v.error ? v.error.code : null,
                msg: v.error ? v.error.message : '',
                mode: v.dataset.mode, w: v.videoWidth, h: v.videoHeight,
                src: (v.currentSrc || '').slice(-34),
                native: v.canPlayType('application/vnd.apple.mpegurl')}; }""")


def run_one(playwright, keyword: str, label: str, ua: str, is_mobile: bool) -> None:
    browser = playwright.chromium.launch(channel="chrome",
                                        args=["--autoplay-policy=no-user-gesture-required"])
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844} if is_mobile else {"width": 1280, "height": 800},
        user_agent=ua, is_mobile=is_mobile, has_touch=is_mobile)
    page = ctx.new_page()
    logs = []
    page.on("console", lambda m: logs.append(f"{m.type}: {m.text[:120]}"))
    page.on("pageerror", lambda e: logs.append(f"pageerror: {str(e)[:120]}"))
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_function('() => document.querySelector("#player").readyState >= 1', timeout=180000)
    page.locator("#hintClose").click()
    page.wait_for_timeout(200)
    page.locator("#sortBtn").click()
    page.locator("#sortMenu button", has_text="按文件名").click()
    page.wait_for_timeout(600)

    box = page.locator("#stage").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    # 按序号精确定位, 不用一路滑过去 (每滑一次都要等一次转码, 很慢)
    order = api("/api/library?sort=folder")["videos"]
    idx = next((i for i, v in enumerate(order) if keyword in v["title"]), -1)
    print(f"    目标 {keyword} 在第 {idx+1}/{len(order)} 个")
    at = int(page.locator("#pos").inner_text().split("/")[0]) - 1
    for _ in range(abs(idx - at)):
        page.mouse.move(cx, cy)
        page.mouse.down()
        for k in range(1, 13):
            page.mouse.move(cx, cy + (220 if idx < at else -220) * k / 12)
        page.mouse.up()
        page.wait_for_function(
            '() => !document.querySelector("#player").paused || '
            'document.querySelector("#toast").hidden === false', timeout=60000)
        page.wait_for_timeout(150)
    name = page.locator("#title").inner_text()
    print(f"    现在播的是 {name}  (native HLS = {probe(page)['native']!r})")
    check(f"[{label}] 定位到目标", keyword in name, name)

    target_dur = order[idx].get("duration") or 0
    watch = int(target_dur) + 6
    samples = []
    for i in range(watch):
        page.wait_for_timeout(1000)
        samples.append(probe(page))
    dur = next((s["dur"] for s in samples if s["dur"] and s["dur"] > 1), 0)
    print("    源时长 %ss, 观测 %ss" % (target_dur, watch))
    print("      t  rs  播   当前    缓冲   解码")
    step = max(1, watch // 12)
    for i in range(0, len(samples), step):
        s = samples[i]
        extra = ""
        if s["err"]:
            extra = "  ERR=%s %s" % (s["err"], s["msg"][:40])
        print("    %3d  %d   %s  %6.2f %6.1f  %dx%d%s"
              % (i, s["rs"], "F" if not s["paused"] else "T",
                 s["t"], s["buf"], s["w"], s["h"], extra))

    first, last = samples[0], samples[-1]
    check("[%s] 开始播放" % label, first["t"] > 0, "t=%s" % first["t"])
    check("[%s] 解码出画面" % label, first["w"] > 0,
          "%dx%d" % (first["w"], first["h"]))
    progressed = last["t"] - first["t"]
    check("[%s] 持续推进" % label, progressed > watch * 0.6,
          "%s 秒内走了 %.1fs" % (watch, progressed))
    # 直播流最容易出的问题: 停在第一片就不动了。
    # 但视频本来就该播完, 播完之后 currentTime 不动是正常的 —— 只在还没结束时查。
    if not last["ended"] and not (target_dur and last["t"] >= target_dur - 1):
        tail = [s["t"] for s in samples[-6:]]
        check("[%s] 末段没有停滞" % label, max(tail) - min(tail) > 1.0,
              "最后 6 秒: %s" % [round(x, 1) for x in tail])
    else:
        print("    (已播到片尾, 跳过停滞检查)")
    errs = [s for s in samples if s["err"]]
    check("[%s] 无解码错误" % label, not errs,
          ("err=%s %s" % (errs[0]["err"], errs[0]["msg"][:60])) if errs else "")
    if target_dur:
        check("[%s] 能播到片尾" % label, last["t"] >= target_dur - 2.5,
              "到 %.1f / %.1f" % (last["t"], target_dur))
    bad = [l for l in logs if "error" in l.lower() or "ERR" in l]
    if bad:
        print(f"    控制台: {'; '.join(bad[:3])}")
    browser.close()


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "IMG_6644"
    print(f"\n  深测 {keyword}")
    with sync_playwright() as p:
        print("  --- 路径 A: hls.js (Android Chrome) ---")
        run_one(p, keyword, "hls.js", ANDROID_UA, True)
        print("  --- 路径 B: 原生 HLS (iPhone Safari) ---")
        run_one(p, keyword, "原生", IPHONE_UA, True)
    print("\n" + "=" * 46)
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
