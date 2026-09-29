"""连续打开多个视频 验收。

复现手机上的真实用法: 看完一个马上点下一个, 页面反复刷新。
重点验证: 不再出现 503 卡死, 每个视频都能真的播起来。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

BASE = f"http://127.0.0.1:{os.environ.get('PORT', '5099')}"
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def api(path: str):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.load(r)


def main() -> int:
    lib = api("/api/library?sort=folder")
    vs = lib["videos"]
    # 挑几个长度差异大的, 模拟"快划过去"
    picks = vs[:6]
    print(f"\n  连开 {len(picks)} 个视频 (每个都重新扫一遍)")

    started = []
    sid = None
    for v in picks:
        req = urllib.request.Request(
            BASE + "/api/hls/start", data=json.dumps({"id": v["id"]}).encode(),
            method="POST", headers={"Content-Type": "application/json", "X-Client-Id": "repro"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                sid = json.load(r)["sid"]
                started.append((v["rel_path"].split("/")[-1], r.status, time.time() - t0))
        except urllib.error.HTTPError as e:
            started.append((v["rel_path"].split("/")[-1], e.code, time.time() - t0))
        # 真实用法: 看完就停掉旧会话再开下一个
        if sid:
            urllib.request.urlopen(urllib.request.Request(
                BASE + "/api/hls/stop", data=json.dumps({"sid": sid}).encode(),
                method="POST",
                headers={"Content-Type": "application/json", "X-Client-Id": "repro"}), timeout=30)
            sid = None
    for name, code, dt in started:
        print(f"    {name:<22} HTTP {code}  {dt:.2f}s")
    check("连续开多个全部成功", all(c == 200 for _, c, _ in started),
          f"{sum(1 for _, c, _ in started if c == 200)}/{len(started)} 个 200")

    # 故意不 stop 就开新的 —— 模拟手机刷新页面 (手机端不会发 stop)
    st = api("/api/media/status")
    print(f"  当前活跃: {len(st['active'])} 个 (状态 {[a['state'] for a in st['active']]})")
    req = urllib.request.Request(
        BASE + "/api/hls/start", data=json.dumps({"id": picks[-1]["id"]}).encode(),
        method="POST", headers={"Content-Type": "application/json", "X-Client-Id": "repro2"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    check("旧会话还在时也能开新的 (不 503)", code == 200, f"HTTP {code}")

    # 真机场景: 浏览器里连续滑动
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
                               timeout=120000)
        page.locator("#hintClose").click()
        page.wait_for_timeout(300)
        box = page.locator("#stage").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

        played = 0
        print("  --- 连续上滑 6 次 ---")
        for i in range(6):
            page.mouse.move(cx, cy)
            page.mouse.down()
            for k in range(1, 13):
                page.mouse.move(cx, cy - 220 * k / 12)
                page.wait_for_timeout(8)
            page.mouse.up()
            try:
                page.wait_for_function(
                    '() => document.querySelector("#player").currentTime > 0', timeout=30000)
                played += 1
                t = page.evaluate('() => document.querySelector("#player").currentTime')
                print(f"    第 {i+1} 次 -> {page.locator('#title').inner_text():<20} 播起来了 t={t:.2f}")
            except Exception:
                toast = page.evaluate(
                    '() => { const x = document.querySelector("#toast");'
                    ' return x.hidden ? "" : x.textContent; }')
                print(f"    第 {i+1} 次 -> {page.locator('#title').inner_text():<20} 没播起来  提示: {toast!r}")
                FAILS.append(f"第 {i+1} 次滑动没播起来")
        check("每次滑动都能播起来", played == 6, f"{played}/6")
        check("没有 JS 报错", not errs, "; ".join(errs[:2]))
        b.close()

    st = api("/api/media/status")
    check("没堆出会话", len(st["active"]) <= 2, f"{len(st['active'])} 个")
    print("\n" + "=" * 46)
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
