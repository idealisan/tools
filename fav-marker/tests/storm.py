"""快速连续切换视频 验收。

复现的问题: 快速滑动时两个 load() 交错, 各开一个转码会话, 其中一个没人停,
播放器可能指到已被停掉的会话 -> "无法播放"。

这里用比人快得多的节奏连切, 看:
  1. 每次都能真的播起来
  2. 不留孤儿会话 (活跃数回到 1)
  3. 服务端日志里没有"会话不存在"的 404
"""

import json
import os
import subprocess
import sys
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
    with urllib.request.urlopen(BASE + path, timeout=180) as r:
        return json.load(r)


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    cadence = int(sys.argv[2]) if len(sys.argv) > 2 else 120   # 毫秒
    lib = api("/api/library?sort=folder")
    for v in lib["videos"]:
        if v.get("direct") is None:
            try:
                api(f"/api/playback/{v['id']}")
            except Exception:
                pass
    lib = api("/api/library?sort=folder")
    print(f"\n  连切 {rounds} 次")
    print(f"  素材: {len(lib['videos'])} 个视频")

    log = os.environ.get("SERVER_LOG", "")
    before = 0
    if log and os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as fh:
            before = len(fh.read())

        played, failed = 0, []
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
                                   timeout=180000)
            page.locator("#hintClose").click()
            page.wait_for_timeout(200)
            box = page.locator("#stage").bounding_box()
            cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

            # 阶段一: 固定节奏猛滑, 不等播放。
            # 竞态就藏在 load() 里那个"等第一个分片"的窗口 (约 0.5~1 秒),
            # 等播起来再滑下一次的话永远撞不上, 必须让滑动落在窗口里。
            print(f"  阶段一: 每 {cadence} 秒滑一次, 连滑 {rounds} 次 (不等播放)")
            for i in range(rounds):
                page.mouse.move(cx, cy)
                page.mouse.down()
                for k in range(1, 7):
                    page.mouse.move(cx, cy - 220 * k / 6)
                page.mouse.up()
                page.wait_for_timeout(cadence)
            peak = len(api("/api/media/status")["active"])
            print(f"    猛滑过程中最多同时有 {peak} 个转码会话")

            # 阶段二: 停下来, 让最后一次装载走完
            print("  阶段二: 停下来等它播完")
            try:
                page.wait_for_function(
                    '() => document.querySelector("#player").currentTime > 0', timeout=40000)
                played = 1
            except Exception:
                toast = page.evaluate(
                    '() => { const x = document.querySelector("#toast");'
                    ' return x.hidden ? "" : x.textContent; }')
                failed.append((0, page.locator("#title").inner_text(), toast))
            name = page.locator("#title").inner_text()
            print(f"    最终停在 {name}  播起来={bool(played)}")

            check("猛滑之后仍能正常播放", played == 1,
                  f"停在 {name}, 提示={failed[0][2]!r}" if failed else name)
            check("没有 JS 报错", not errs, "; ".join(errs[:2]))
            b.close()

    # 静置一下, 确认没有孤儿会话堆着
    time.sleep(3)
    st = api("/api/media/status")
    check("没堆出孤儿会话", len(st["active"]) <= 2,
          f"猛滑时最多 {peak} 个, 静置后 {len(st['active'])} 个")
    ffmpeg = subprocess.run(["pgrep", "-f", "ffmpeg.*favhls"],
                            capture_output=True).stdout.decode().split()
    check("ffmpeg 进程没堆起来", len(ffmpeg) <= 2, f"{len(ffmpeg)} 个")

    if log and os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as fh:
            new = fh.read()[before:]
        gone = new.count("会话不存在")
        noseg = new.count("等不到分片")
        print(f"\n  服务端 404 原因统计: 会话不存在 {gone} 次, 等不到分片 {noseg} 次")
        # "等不到分片" 才是"无法播放"的真正症状: 一堆 load 交错, 十几条
        # ffmpeg 抢 CPU, 谁也来不及在 3 秒内吐出第一个分片
        check("没有等不到分片的 404", noseg == 0, f"{noseg} 次")
        check("没有孤儿会话 404", gone == 0, f"{gone} 次")

    print("\n" + "=" * 46)
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
