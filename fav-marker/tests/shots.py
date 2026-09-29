"""截几张界面图, 用来肉眼检查排版。用 tests/with_server.sh 起服务。"""

import os

from playwright.sync_api import sync_playwright

BASE = f"http://127.0.0.1:{os.environ.get('PORT', '5099')}"
OUT = os.environ.get("SHOTS", "/tmp/favtest")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome", args=["--autoplay-policy=no-user-gesture-required"]
        )
        page = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True, has_touch=True, device_scale_factor=2,
        ).new_page()
        page.goto(BASE, wait_until="domcontentloaded")
        # 等真的开始播; networkidle 在大文件流式播放时永远不成立
        page.wait_for_function(
            '() => document.querySelector("#player").readyState >= 1', timeout=60000)
        page.wait_for_timeout(1200)
        page.screenshot(path=f"{OUT}/1-首次进入.png")

        page.locator("#hintClose").click()
        page.wait_for_timeout(400)
        box = page.locator("#stage").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

        # 滑到一半停住, 检查方向提示
        page.mouse.move(cx, cy)
        page.mouse.down()
        for k in range(1, 10):
            page.mouse.move(cx - 90 * k / 9, cy)
            page.wait_for_timeout(12)
        page.wait_for_timeout(200)
        page.screenshot(path=f"{OUT}/2-左滑提示.png")
        page.mouse.up()
        page.wait_for_timeout(1000)
        page.screenshot(path=f"{OUT}/3-标记之后.png")

        page.locator("#sortBtn").click()
        page.wait_for_timeout(400)
        page.screenshot(path=f"{OUT}/4-排序菜单.png")
        page.keyboard.press("Escape")
        page.mouse.click(cx, 20)
        page.wait_for_timeout(300)

        # 暂停态
        page.evaluate("() => document.querySelector('#player').pause()")
        page.wait_for_timeout(400)
        page.screenshot(path=f"{OUT}/5-暂停.png")
        browser.close()
    print(f"截图已写入 {OUT}/")


if __name__ == "__main__":
    main()
