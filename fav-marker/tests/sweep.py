"""把整个片库逐个播一遍, 找出播不了的。

手机上手动翻的时候看到"无法播放", 这里就用同样的方式自动走一遍,
把失败的视频列出来, 方便定位到具体是哪些文件、什么原因。
"""

import json
import os
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

BASE = f"http://127.0.0.1:{os.environ.get('PORT', '5099')}"
FAILS: list[str] = []


def api(path: str):
    with urllib.request.urlopen(BASE + path, timeout=180) as r:
        return json.load(r)


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    lib = api("/api/library?sort=folder")
    vs = lib["videos"]
    if limit:
        vs = vs[:limit]
    print(f"\n  逐个播放 {len(vs)} 个视频"
          f"{' (前 %d 个)' % limit if limit else ''}")

    # 先全部探测一遍, 免得播的时候 ffprobe 抢时间
    t0 = time.time()
    for v in vs:
        if v.get("direct") is None:
            try:
                api(f"/api/playback/{v['id']}")
            except Exception:
                pass
    print(f"  探测 {len(vs)} 个用了 {time.time()-t0:.1f}s")

    ok, failed, slow = [], [], []
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
        for i in range(len(vs) - 1):
            try:
                page.wait_for_function(
                    '() => document.querySelector("#player").currentTime > 0 || '
                    'document.querySelector("#player").ended', timeout=25000)
            except Exception:
                pass
            st = page.evaluate("""() => { const v = document.querySelector('#player');
                return {t: v.currentTime, ended: v.ended, dur: v.duration, rs: v.readyState,
                        mode: v.dataset.mode, err: v.error && v.error.code,
                        title: document.querySelector('#title').textContent,
                        toast: (() => { const x = document.querySelector('#toast');
                                 return x.hidden ? '' : x.textContent; })()}; }""")
            name = st["title"]
            if st["t"] > 0 or st["ended"]:
                ok.append(name)
            else:
                failed.append((name, st["rs"], st["err"], st["toast"]))
                print(f"    [{i+1}/{len(vs)}] 播不了: {name:<18} "
                      f"rs={st['rs']} err={st['err']} 提示={st['toast']!r}")
            # 往下一个
            page.mouse.move(cx, cy)
            page.mouse.down()
            for k in range(1, 13):
                page.mouse.move(cx, cy - 220 * k / 12)
                page.wait_for_timeout(6)
            page.mouse.up()
            page.wait_for_timeout(250)
        b.close()

    print(f"\n  能播 {len(ok)} / 播不了 {len(failed)} / 共 {len(vs)}")
    if failed:
        print("  播不了的:")
        for name, rs, err, toast in failed:
            print(f"    {name:<20} readyState={rs} error={err} 提示={toast!r}")
    if errs:
        print(f"  JS 报错 {len(errs)} 条: " + "; ".join(errs[:3]))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
