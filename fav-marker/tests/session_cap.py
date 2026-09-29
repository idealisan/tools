"""并发转码会话上限 验收。"""

import json
import os
import urllib.error
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('PORT', '5099')}"


def post(path: str, payload: dict, cid: str = "cap-test"):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Client-Id": cid},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> int:
    MAX = json.load(urllib.request.urlopen(BASE + "/api/media/status",
                                            timeout=30)).get("max_sessions", 1)
    print(f"  服务端同时只允许 {MAX} 个转码会话")
    lib = json.load(urllib.request.urlopen(BASE + "/api/library", timeout=30))
    need = [v["id"] for v in lib["videos"] if not v.get("direct")]
    print(f"\n  需要转码的视频: {len(need)} 个")
    if len(need) < MAX:
        print(f"  跳过: 需要至少 {MAX} 个待转码视频")
        return 0

    results = []
    for vid in need:
        status, body = post("/api/hls/start", {"id": vid})
        results.append((status, body.get("sid") or body.get("error")))
    for i, (status, info) in enumerate(results, 1):
        print(f"    第 {i} 个 -> HTTP {status}  {info}")

    ok = sum(1 for s, _ in results if s == 200)
    limited = sum(1 for s, _ in results if s == 503)
    good = ok == MAX and limited == len(results) - MAX
    print(f"\n  {'PASS' if good else 'FAIL'}  "
          f"上限 {MAX} 个: 放行 {ok}, 拦下 {limited}")
    if not good:
        return 1

    print("  收尾: 停掉全部会话")
    for _, info in results:
        if info and not str(info).startswith("转码任务"):
            post("/api/hls/stop", {"sid": info})
    st = json.load(urllib.request.urlopen(BASE + "/api/media/status", timeout=30))
    print(f"    剩余会话: {len(st['active'])}")
    return 0 if len(st["active"]) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
