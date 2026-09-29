"""连播 (自动下一个) 验收。"""

import os

from playwright.sync_api import sync_playwright

BASE = f"http://127.0.0.1:{os.environ.get('PORT', '5099')}"
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome", args=["--autoplay-policy=no-user-gesture-required"]
        )
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
        )
        page = ctx.new_page()
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_function(
            '() => document.querySelector("#player").readyState >= 1', timeout=60000
        )
        page.locator("#hintClose").click()
        page.wait_for_timeout(300)
        page.locator("#sortBtn").click()
        page.locator("#sortMenu button", has_text="按文件名").click()
        page.wait_for_timeout(800)

        box = page.locator("#stage").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

        def swipe(dy: int = -220) -> None:
            page.mouse.move(cx, cy)
            page.mouse.down()
            for k in range(1, 13):
                page.mouse.move(cx, cy + dy * k / 12)
                page.wait_for_timeout(8)
            page.mouse.up()
            page.wait_for_timeout(800)

        def goto(name: str) -> tuple[str, str]:
            for _ in range(10):
                if page.locator("#title").inner_text() == name:
                    break
                swipe()
            return page.locator("#title").inner_text(), page.locator("#pos").inner_text()

        def wait_playing(timeout: int = 60000) -> bool:
            try:
                page.wait_for_function(
                    '() => document.querySelector("#player").currentTime > 0', timeout=timeout
                )
                return True
            except Exception:
                return False

        def skip_to_end(frac: float = 0.98, tries: int = 10) -> bool:
            """把播放头拖到接近片尾, 快速触发 ended。

            不用等自然播完 —— 素材里有 10 分钟的片子, 等不起。
            """
            bar = page.locator("#bar-progress").bounding_box()
            y = bar["y"] + bar["height"] / 2
            for _ in range(tries):
                page.mouse.move(bar["x"] + 14, y)
                page.mouse.down()
                page.mouse.move(bar["x"] + 14 + (bar["width"] - 28) * frac, y)
                page.wait_for_timeout(200)
                page.mouse.up()
                try:
                    page.wait_for_function(
                        '() => document.querySelector("#player").ended', timeout=20000)
                    return True
                except Exception:
                    page.wait_for_timeout(500)
            return False

        title, pos = goto("direct")
        at, total = (int(x) for x in pos.split("/"))
        check("定位到 direct.mp4", title == "direct", f"{title} @ {pos}")
        check("不在最后一个（连播才有意义）", at < total, pos)

        print("  --- 连播关: 播完停在原地 ---")
        check("默认关闭", page.locator("#autoBtn").get_attribute("aria-pressed") == "false",
              page.locator("#autoBtn").inner_text())
        check("等它播完", skip_to_end())
        page.wait_for_timeout(2500)
        check("连播关: 没有自动切走", page.locator("#title").inner_text() == "direct",
              page.locator("#title").inner_text())

        print("  --- 连播开: 正在播的时候打开 ---")
        before = page.locator("#title").inner_text()      # 必须在点击之前读
        page.locator("#autoBtn").click()
        page.wait_for_timeout(200)
        check("按钮变开", page.locator("#autoBtn").get_attribute("aria-pressed") == "true",
              page.locator("#autoBtn").inner_text())
        # 上一个已经看完停住了, 打开连播应该立刻往前走
        page.wait_for_function('() => document.querySelector("#player").ended === false',
                               timeout=15000)
        check("打开连播后立即往下走", page.locator("#title").inner_text() != before,
              f"{before} -> {page.locator('#title').inner_text()}")
        mid = page.locator("#title").inner_text()
        check("新片子真的开始播", wait_playing(),
              page.evaluate("""() => { const v = document.querySelector('#player');
                  return `mode=${v.dataset.mode} t=${v.currentTime.toFixed(2)} rs=${v.readyState}`; }"""))
        # 这次让它播完, 应该自动切到再下一个
        check("能把它播完", skip_to_end())
        page.wait_for_timeout(3000)
        after = page.locator("#title").inner_text()
        check("播完自动进入下一个", after != mid, f"{mid} -> {after}")
        check("再下一个也开播了", wait_playing())

        print("  --- 连播关回去 ---")
        page.locator("#autoBtn").click()
        page.wait_for_timeout(200)
        check("按钮变回关", page.locator("#autoBtn").get_attribute("aria-pressed") == "false",
              page.locator("#autoBtn").inner_text())
        browser.close()

    print("\n" + "=" * 44)
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
