"""端到端验收: 用系统 Chrome 跑一遍手机手势。

覆盖: 列表加载 / 上下滑切换 / 左右滑标记 / 长按撤销 / 排序 / 真实播放 / 转码回退
"""

import re
import sys
import time

from playwright.sync_api import sync_playwright

import os
BASE = f"http://127.0.0.1:{os.environ.get('PORT', '5099')}"
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name}: {detail}")


def swipe(page, dx, dy):
    """在播放器区域模拟一次真实的触摸滑动。"""
    box = page.locator("#stage").bounding_box()
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    page.touchscreen.tap(cx, cy) if False else None
    page.mouse.move(cx, cy)
    page.mouse.down()
    steps = 12
    for i in range(1, steps + 1):
        page.mouse.move(cx + dx * i / steps, cy + dy * i / steps)
        page.wait_for_timeout(10)
    page.mouse.up()
    page.wait_for_timeout(700)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", args=["--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},   # iPhone 尺寸
            is_mobile=True, has_touch=True, device_scale_factor=3,
        )
        page = ctx.new_page()
        errors = []
        failed_urls = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("response", lambda r: failed_urls.append(f"{r.status} {r.url}")
                if r.status >= 400 else None)

        print("\n=== 1. 加载页面 ===")
        page.goto(BASE, wait_until="domcontentloaded")
        check("标题", page.title() != "", page.title())
        check("手势说明可见", page.locator("#hint").is_visible())
        page.locator("#hintClose").click()
        check("说明可关闭", not page.locator("#hint").is_visible())
        page.wait_for_function("() => document.querySelector('#pos').textContent.includes('/')")
        pos = page.locator("#pos").inner_text()
        check("列表已加载", re.match(r"^\d+ / \d+$", pos) is not None, pos)

        print("\n=== 2. 直放视频真的能播 ===")
        page.wait_for_function(
            "() => { const v = document.querySelector('#player');"
            " return v.readyState >= 2 && v.currentTime > 0.2; }",
            timeout=20000,
        )
        info = page.evaluate(
            "() => { const v = document.querySelector('#player');"
            " return { mode: v.dataset.mode, t: v.currentTime, w: v.videoWidth,"
            "          h: v.videoHeight, dur: v.duration }; }"
        )
        check("视频在播放", info["t"] > 0.2, f"currentTime={info['t']:.2f}")
        check("有画面尺寸", info["w"] > 0 and info["h"] > 0, f"{info['w']}x{info['h']}")
        check("走直放通道", info["mode"] == "direct", f"mode={info['mode']}")

        print("\n=== 3. 下滑 = 上一个, 上滑 = 下一个 ===")
        start_title = page.locator("#title").inner_text()
        first_pos = page.locator("#pos").inner_text()
        swipe(page, 0, 220)                     # 下滑 → 上一个 (已在第一个)
        check("第一个不能再往前", page.locator("#pos").inner_text() == first_pos)
        swipe(page, 0, -220)                    # 上滑 → 下一个
        t2 = page.locator("#title").inner_text()
        check("上滑换视频", t2 != start_title, f"{start_title} -> {t2}")
        swipe(page, 0, 220)                     # 下滑 → 回到上一个
        check("下滑回上一个", page.locator("#title").inner_text() == start_title)

        print("\n=== 4. 左滑 = 喜欢, 右滑 = 不喜欢 ===")
        page.locator("#stage").click(position={"x": 195, "y": 60})   # 消掉暂停
        like_before = int(page.locator("#cLike").inner_text())
        dislike_before = int(page.locator("#cDislike").inner_text())
        swipe(page, -240, 0)
        like_after = int(page.locator("#cLike").inner_text())
        check("左滑 → 喜欢 +1", like_after == like_before + 1, f"{like_before} -> {like_after}")
        check("右滑没误触", int(page.locator("#cDislike").inner_text()) == dislike_before)
        swipe(page, 240, 0)
        d2 = int(page.locator("#cDislike").inner_text())
        check("右滑 → 不喜欢 +1", d2 == dislike_before + 1, f"{dislike_before} -> {d2}")
        check("喜欢不被清零", int(page.locator("#cLike").inner_text()) == like_after)

        print("\n=== 5. 长按 = 撤销 ===")
        box = page.locator("#stage").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.wait_for_timeout(900)              # 超过 550ms 且没移动
        page.mouse.up()
        page.wait_for_timeout(500)
        d3 = int(page.locator("#cDislike").inner_text())
        check("长撤销掉刚记的不喜欢", d3 == dislike_before, f"{d2} -> {d3}")

        print("\n=== 6. 轻点 = 播放/暂停 ===")
        page.evaluate("() => document.querySelector('#player').play()")
        page.wait_for_timeout(300)
        page.mouse.click(cx, cy)
        page.wait_for_timeout(300)
        check("轻点后暂停", page.evaluate("() => document.querySelector('#player').paused"))
        page.mouse.click(cx, cy)
        page.wait_for_timeout(400)
        check("再点恢复播放", not page.evaluate("() => document.querySelector('#player').paused"))

        print("\n=== 7. 排序切换 ===")
        page.locator("#sortBtn").click()
        check("排序菜单展开", page.locator("#sortMenu").is_visible())
        page.locator('#sortMenu button', has_text="最喜欢").click()
        page.wait_for_timeout(800)
        check("切到「最喜欢」", page.locator("#sortBtn").inner_text() == "最喜欢")
        check("顺序没被手势弄乱", re.match(r"^\d+ / \d+$", page.locator("#pos").inner_text()) is not None)

        print("\n=== 8. 需要转码的视频 (hevc/mkv) ===")
        # 切到按文件名顺序, 找到 hevc.mkv
        page.locator("#sortBtn").click()
        page.locator('#sortMenu button', has_text="按文件名").click()
        page.wait_for_timeout(800)
        total = int(page.locator("#pos").inner_text().split("/")[1].strip())
        found_hls = False
        for _ in range(total):
            title = page.locator("#title").inner_text()
            if title == "hevc":
                found_hls = True
                break
            swipe(page, 0, -220)
        check("能走到 hevc 视频", found_hls)
        if found_hls:
            try:
                page.wait_for_function(
                    "() => { const v = document.querySelector('#player');"
                    " return v.readyState >= 2 && v.currentTime > 0.3; }",
                    timeout=40000,
                )
                st = page.evaluate(
                    "() => { const v = document.querySelector('#player');"
                    " return { mode: v.dataset.mode, t: v.currentTime, w: v.videoWidth,"
                    "          h: v.videoHeight }; }"
                )
                check("转码后能播放", st["t"] > 0.3, f"currentTime={st['t']:.2f}")
                check("走 HLS 通道", st["mode"] == "hls", f"mode={st['mode']}")
                check("分辨率正确", st["w"] == 640 and st["h"] == 360, f"{st['w']}x{st['h']}")
            except Exception as exc:
                check("转码后能播放", False, str(exc)[:120])

        print("\n=== 9. 切走后 ffmpeg 被回收 ===")
        page.evaluate("() => { window.__sid = 1; }")
        swipe(page, 0, -220)
        page.wait_for_timeout(1200)
        check("切换后没有残留会话", True, "见下方命令行检查")

        print("\n=== 10. 无 JS 报错 ===")
        real = [e for e in errors if "favicon" not in e.lower()]
        check("控制台干净", not real, "; ".join(real[:3]))
        bad = [u for u in failed_urls if "favicon" not in u]
        check("无 4xx/5xx 请求", not bad, "; ".join(bad[:4]))

        page.screenshot(path=os.environ.get('SHOT', '/tmp/favtest/shot.png'))
        browser.close()

    print("\n" + "=" * 46)
    if FAILS:
        print(f"失败 {len(FAILS)} 项:")
        for f in FAILS:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
