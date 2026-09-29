"""后端逻辑验收: 排序、统计、扫描、路径安全。

直接打 HTTP 接口, 不开浏览器, 跑得快。
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('PORT', '5099')}"
CID = "unit-test-client"
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name}: {detail}")


def get(path: str, cid: str = CID) -> dict:
    req = urllib.request.Request(BASE + path, headers={"X-Client-Id": cid})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def post(path: str, payload: dict, cid: str = CID) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Client-Id": cid}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"__status__": e.code, **json.loads(e.read() or b"{}")}


def code(path: str, method: str = "GET", headers: dict | None = None) -> int:
    req = urllib.request.Request(BASE + path, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def main() -> int:
    lib = get("/api/library?sort=folder")
    vids = lib["videos"]
    print(f"\n=== 基础 ===")
    check("库里有视频", len(vids) > 0, f"{len(vids)} 个")
    check("带 ffmpeg 能力标记", "ffmpeg" in lib, f"ffmpeg={lib.get('ffmpeg')}")
    check("有 health 接口", get("/api/health")["ok"] is True)

    print("\n=== 排序 ===")
    for sort in ("wilson", "likes", "disliked", "recent", "unvoted", "folder"):
        d = get(f"/api/library?sort={sort}")
        check(f"排序 {sort} 可用", d["sort"] == sort, f"返回 {d['total']} 个")
    check("非法排序回落到 folder", get("/api/library?sort=../../etc")["sort"] == "folder")

    # wilson 排序必须真的按分数降序
    w = get("/api/library?sort=wilson")["videos"]
    scores = [v["wilson"] for v in w]
    check("Wilson 降序", scores == sorted(scores, reverse=True),
          " ".join(f"{s:.3f}" for s in scores[:6]))

    print("\n=== 喜欢 / 不喜欢 记录 ===")
    v0, v1 = vids[0], vids[1] if len(vids) > 1 else vids[0]
    base0 = v0["like"] + v0["dislike"]
    r = post("/api/vote", {"id": v0["id"], "action": "like"})
    check("喜欢 +1", r["video"]["like"] + r["video"]["dislike"] == base0 + 1,
          f"{base0} -> {r['video']['like'] + r['video']['dislike']}")
    r = post("/api/vote", {"id": v0["id"], "action": "like"})
    check("再喜欢一次仍 +1（只增不减）",
          r["video"]["like"] + r["video"]["dislike"] == base0 + 2)
    r = post("/api/undo", {"id": v0["id"]})
    check("撤销 -1", r["undone"] is True and
          r["video"]["like"] + r["video"]["dislike"] == base0 + 1)
    post("/api/undo", {"id": v0["id"]})
    post("/api/undo", {"id": v0["id"]})
    post("/api/undo", {"id": v0["id"]})
    check("撤到底也不报错", post("/api/undo", {"id": v0["id"]})["undone"] is False)

    print("\n=== 跨设备隔离 ===")
    a = post("/api/vote", {"id": v0["id"], "action": "like"}, cid="devA")
    b = post("/api/vote", {"id": v0["id"], "action": "dislike"}, cid="devB")
    check("两台设备的记录都留着",
          b["video"]["like"] >= a["video"]["like"] and b["video"]["dislike"] >= 1)
    check("A 看不到 B 的操作数",
          get("/api/library?sort=folder", "devA")["videos"][0]["my_dislike"] != 1)
    post("/api/undo", {"id": v0["id"]}, cid="devA")
    post("/api/undo", {"id": v0["id"]}, cid="devB")

    print("\n=== 错误处理 ===")
    check("缺 Client-Id 拒绝", post("/api/vote", {"id": v0["id"], "action": "like"}, cid="")["__status__"] == 400)
    check("未知动作拒绝", post("/api/vote", {"id": v0["id"], "action": "love"})["__status__"] == 400)
    check("不存在的视频 404", post("/api/vote", {"id": "deadbeef", "action": "like"})["__status__"] == 404)
    check("不存在的视频播放信息 404", code("/api/playback/deadbeef") == 404)

    print("\n=== 路径安全 ===")
    check("任意 media id 404", code("/media/../../../../etc/passwd") in (400, 404))
    check("不存在的 media 404", code("/media/0123456789abcdef") == 404)
    check("未知 hls 会话 404", code("/hls/xyz/index.m3u8") == 404)
    check("分片目录穿越被挡", code("/hls/xyz/..%2f..%2fetc%2fpasswd.ts") == 404)
    check("伪造分片名被挡", code("/hls/xyz/etc.ts") == 404)
    check("start 参数为负会归零",
          post("/api/hls/start", {"id": v1["id"], "start": -5}).get("start") == 0.0)

    print("\n=== 统计口径 ===")
    st = get("/api/stats")
    check("视频数对得上", st["videos"] == len(vids), f"{st['videos']} vs {len(vids)}")
    check("已标记不超过总数", st["marked"] <= st["videos"], f"{st['marked']}/{st['videos']}")

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
