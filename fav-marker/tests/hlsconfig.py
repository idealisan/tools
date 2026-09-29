"""hls.js 参数实测: 哪种配置能既从 0 起播、又不往前追。

转码比播放快 7 倍, 播放列表又是"一直变长"的, hls.js 默认会贴着流末尾播,
把开头整段跳过去。这里把几组配置都试一遍, 看哪组能用。
"""

import json
import os
import time
import urllib.request

from playwright.sync_api import sync_playwright

BASE = f"http://127.0.0.1:{os.environ.get('PORT', '5099')}"

CONFIGS = [
    ("liveSync0 + 默认 start", '{"liveSyncDurationCount":0,"maxBufferLength":12}'),
    ("liveSync0 + startPos0", '{"liveSyncDurationCount":0,"startPosition":0,"maxBufferLength":12}'),
    ("liveSync9999 + startPos0", '{"liveSyncDurationCount":9999,"startPosition":0,"maxBufferLength":12}'),
    ("liveSync3 + startPos0", '{"liveSyncDurationCount":3,"startPosition":0,"maxBufferLength":12}'),
]


def api(path: str):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.load(r)


def post(path: str, payload: dict):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Client-Id": "cfg"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def probe_all():
    lib = api("/api/library?sort=folder")
    for v in lib["videos"]:
        if v.get("direct") is None:
            try:
                api(f"/api/playback/{v['id']}")
            except Exception:
                pass
    lib = api("/api/library?sort=folder")
    cand = [v for v in lib["videos"] if v.get("direct") is False and (v.get("duration") or 0) > 100]
    return max(cand, key=lambda v: v["duration"]) if cand else None


def main() -> int:
    target = probe_all()
    if not target:
        print("没有足够长的待转码视频, 跳过")
        return 0
    print(f"  目标: {target['title']} ({target['duration']:.0f} 秒)\n")

    rows = []
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome",
                              args=["--autoplay-policy=no-user-gesture-required"])
        ctx = b.new_context(viewport={"width": 390, "height": 844},
                            is_mobile=True, has_touch=True)
        page = ctx.new_page()
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_function('() => document.querySelector("#player").readyState >= 1',
                               timeout=60000)

        for name, cfg in CONFIGS:
            # 每个配置都用一个全新的会话, 转到"超车"状态再接
            info = post("/api/hls/start", {"id": target["id"]})
            sid = info["sid"]
            edge = 0
            for _ in range(120):
                w = api(f"/api/hls/where?sid={sid}")
                edge = w["produced_end"]
                if edge > 40:
                    break
                time.sleep(0.5)

            page.evaluate("""async ([pl, cfgText]) => {
                const v = document.querySelector('#player');
                if (window.__hls) { try { window.__hls.destroy(); } catch (e) {} }
                window.__log = [];
                const cfg = Object.assign({enableWorker: true}, JSON.parse(cfgText));
                const hls = new Hls(cfg);
                window.__hls = hls;
                hls.on(Hls.Events.ERROR, (e, d) => { if (d.fatal) window.__log.push('ERR ' + d.details); });
                hls.loadSource(pl);
                hls.attachMedia(v);
                await new Promise(r => setTimeout(r, 200));
                await v.play().catch(e => window.__log.push('play:' + e.name));
            }""", [f"/hls/{sid}/index.m3u8", cfg])

            page.wait_for_function(
                '() => document.querySelector("#player").currentTime > 0', timeout=45000)
            page.wait_for_timeout(400)
            start = page.evaluate('() => document.querySelector("#player").currentTime')
            samples = [start]
            for _ in range(6):
                page.wait_for_timeout(1200)
                samples.append(page.evaluate('() => document.querySelector("#player").currentTime'))
            errs = page.evaluate('() => window.__log')
            # 追不追流末尾: 这 7 秒里实际播了多长 (理想 ≈ 7)
            advance = samples[-1] - samples[0]
            rows.append((name, start, edge, advance, errs))
            post("/api/hls/stop", {"sid": sid})
            page.wait_for_timeout(400)

        b.close()

    print(f"  {'配置':<28}{'起播位置':>10}{'流末尾':>9}{'7秒内推进':>12}   评价")
    print("  " + "-" * 78)
    for name, start, edge, adv, errs in rows:
        ok_start = start < 5
        ok_adv = 5.0 <= adv <= 9.0
        verdict = "可用" if (ok_start and ok_adv and not errs) else \
                  ("起播跳到末尾" if not ok_start else
                   ("会往前追/卡" if not ok_adv else "有错误"))
        print(f"  {name:<28}{start:>9.1f}s{edge:>8.0f}s{adv:>11.1f}s   {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
