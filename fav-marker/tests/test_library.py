"""library.py 的单元测试: 评分、排序、扫描、投票语义。

直接建临时目录跑, 不需要服务器。
"""

import os
import shutil
import sys
import tempfile

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from library import Library, natural_key, preference_label, video_id_for, wilson_lower  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name}: {detail}")


def touch(path: Path, size: int = 1024) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)


def tmp_library() -> tuple[Library, Path]:
    root = Path(tempfile.mkdtemp(prefix="favlib-"))
    lib = Library([str(root)], str(root / "db" / "fav.db"))
    return lib, root


def test_wilson() -> None:
    print("\n=== Wilson 评分下界 ===")
    check("无记录为 0", wilson_lower(0, 0) == 0.0)
    check("纯喜欢也低于 1（因为有不确定性）", 0 < wilson_lower(50, 0) < 1)

    # 同样 100% 好评, 记录越少越不可信
    few = wilson_lower(1, 0)
    many = wilson_lower(100, 0)
    check("同样全赞, 记录多的分更高", few < many, f"1 次={few:.3f} < 100 次={many:.3f}")

    # 1 次喜欢 vs 30赞10踩 —— 这正是 Wilson 存在的意义
    check("1 次喜欢 排在 30赞10踩 之后",
          wilson_lower(1, 0) < wilson_lower(30, 10),
          f"1赞={wilson_lower(1,0):.3f} < 30赞10踩={wilson_lower(30,10):.3f}")

    # 相同好评率下, 样本多的更靠前
    check("同为 70% 好评, 样本多的分更高",
          wilson_lower(21, 9) > wilson_lower(2, 1),
          f"21/9={wilson_lower(21,9):.3f} > 2/1={wilson_lower(2,1):.3f}")

    check("全踩得 0 分", wilson_lower(0, 20) == 0.0)
    check("分数不会为负", wilson_lower(0, 5) >= 0.0)
    check("对称性: 赞踩互换后不同（说明不是净好评率）",
          wilson_lower(10, 3) != wilson_lower(3, 10))


def test_labels() -> None:
    print("\n=== 结论标签 ===")
    check("未标记", preference_label(0, 0, 0.0) == "未标记")
    check("全喜欢", preference_label(5, 0, wilson_lower(5, 0)) == "全喜欢")
    # 30赞10踩 = 0.598, 明显正向但不算「最爱」, 应该是「比较喜欢」
    check("30赞10踩 → 比较喜欢", preference_label(30, 10, wilson_lower(30, 10)) == "比较喜欢",
          f"{wilson_lower(30, 10):.3f}")
    check("压倒性好评 → 最喜欢", preference_label(100, 5, wilson_lower(100, 5)) == "最喜欢",
          f"{wilson_lower(100, 5):.3f}")
    check("不太喜欢", preference_label(1, 9, wilson_lower(1, 9)) == "不太喜欢")
    check("标签非空", all(preference_label(*a) for a in
                       [(0, 0, 0.0), (1, 0, .2), (0, 1, 0.0), (5, 5, .5)]))


def test_natural_sort() -> None:
    print("\n=== 自然排序 ===")
    names = ["a10.mp4", "a2.mp4", "a1.mp4", "B.mp4", "a20.mp4"]
    got = sorted(names, key=natural_key)
    check("a1 a2 a10 a20", got == ["a1.mp4", "a2.mp4", "a10.mp4", "a20.mp4", "B.mp4"], str(got))


def test_scan() -> None:
    print("\n=== 扫描 ===")
    lib, root = tmp_library()
    try:
        (root / "videos").mkdir()
        touch(root / "videos" / "a.mp4")
        os.makedirs(root / "a" / "b" / "c", exist_ok=True)
        touch(root / "a" / "b" / "c" / "deep.mkv")
        touch(root / "notes.txt")            # 非视频
        touch(root / ".hidden.mp4")          # 隐藏文件
        touch(root / "movie.avi")
        st = lib.scan()
        check("递归找到 3 个视频", st["total"] == 3, f"{st['total']} 个: " +
              ", ".join(v.rel_path for v in lib.list_videos()))
        rels = {v.rel_path for v in lib.list_videos()}
        check("扫到深层目录", "a/b/c/deep.mkv" in rels)
        check("忽略非视频", not any(r.endswith(".txt") for r in rels))
        check("忽略隐藏文件", not any("hidden" in r for r in rels))
        check("统计新增数", st["added"] == 3 and st["removed"] == 0, str(st))

        # 重复扫描应幂等
        st2 = lib.scan()
        check("重复扫描幂等", st2["total"] == 3 and st2["added"] == 0 and st2["removed"] == 0, str(st2))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_rename_keeps_history() -> None:
    print("\n=== 改名 / 移动 保留历史 ===")
    lib, root = tmp_library()
    try:
        f = root / "old_name.mp4"
        touch(f, 5000)
        lib.scan()
        vid = video_id_for(f)
        for _ in range(3):
            lib.vote(vid, "like", "c1")
        check("攒了 3 次喜欢", lib.get(vid, "c1").like == 3)

        dest = root / "归档" / "2024" / "新名字.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(f, dest)                       # 换名 + 挪目录
        st = lib.scan()
        check("识别为改名而非删除+新增", st["renamed"] == 1 and st["added"] == 0 and st["removed"] == 0, str(st))

        new_vid = video_id_for(dest)
        got = lib.get(new_vid, "c1")
        check("喜欢记录跟到了新名字", got is not None and got.like == 3, f"like={got and got.like}")

        # 真删除
        os.remove(dest)
        st = lib.scan()
        check("真删除会清掉视频行", st["removed"] == 1 and st["total"] == 0, str(st))
        check("删除后统计不计入孤儿记录", lib.stats()["likes"] == 0, str(lib.stats()))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_vote_semantics() -> None:
    print("\n=== 投票语义 ===")
    lib, root = tmp_library()
    try:
        f = root / "v.mp4"
        touch(f)
        lib.scan()
        vid = video_id_for(f)

        lib.vote(vid, "like", "p")
        lib.vote(vid, "like", "p")
        v = lib.get(vid, "p")
        check("重复喜欢累加而非抵消", v.like == 2, f"like={v.like}")

        lib.vote(vid, "dislike", "p")
        v = lib.get(vid, "p")
        check("改标记不抹掉旧的", v.like == 2 and v.dislike == 1, f"{v.like}/{v.dislike}")

        r = lib.undo(vid, "p")
        check("撤销最近一次", r["undone"] is True)
        v = lib.get(vid, "p")
        check("撤销后计数正确", v.like == 2 and v.dislike == 0, f"{v.like}/{v.dislike}")

        r = lib.undo(vid, "other-device")
        check("别人的记录撤不掉", r["undone"] is False)
        check("数据没被影响", lib.get(vid, "p").like == 2)

        try:
            lib.vote(vid, "love", "p")
            check("非法动作应报错", False, "居然没抛异常")
        except ValueError:
            check("非法动作应报错", True)
        try:
            lib.vote("nope", "like", "p")
            check("不存在的视频应报错", False, "居然没抛异常")
        except KeyError:
            check("不存在的视频应报错", True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_sorting_modes() -> None:
    print("\n=== 排序模式 ===")
    lib, root = tmp_library()
    try:
        names = ["v1.mp4", "v2.mp4", "v3.mp4", "v4.mp4"]
        for n in names:
            touch(root / n)
        lib.scan()
        ids = {v.rel_path: v.id for v in lib.list_videos("folder")}

        # v1: 1 次喜欢        -> 看着 100% 但样本太少
        lib.vote(ids["v1.mp4"], "like", "c")
        # v2: 30赞10踩        -> 真实好评
        for _ in range(30):
            lib.vote(ids["v2.mp4"], "like", "c")
        for _ in range(10):
            lib.vote(ids["v2.mp4"], "dislike", "c")
        # v3: 5赞20踩
        for _ in range(5):
            lib.vote(ids["v3.mp4"], "like", "c")
        for _ in range(20):
            lib.vote(ids["v3.mp4"], "dislike", "c")
        # v4: 没标记

        w = [v.title for v in lib.list_videos("wilson")]
        check("Wilson: 30赞10踩 排最前", w[0] == "v2", f"-> {w}")
        check("Wilson: 没标记的排最后", w[-1] == "v4", f"-> {w}")
        check("Wilson: 1次喜欢不排第一", w.index("v1") > 0, f"-> {w}")

        un = [v.title for v in lib.list_videos("unvoted")]
        check("未标记筛选只剩 v4", un == ["v4"], f"-> {un}")
        rec = [v.title for v in lib.list_videos("recent")]
        check("久未看: 从没标记的排最前", rec[0] == "v4", f"-> {rec}")

        lk = [v.title for v in lib.list_videos("likes")]
        check("喜欢最多: v2 第一", lk[0] == "v2", f"-> {lk}")

        dk = [v.title for v in lib.list_videos("disliked")]
        check("不喜欢最多: v3 第一", dk[0] == "v3", f"-> {dk}")

        check("名字排序按自然序",
              [v.title for v in lib.list_videos("folder")] == ["v1", "v2", "v3", "v4"])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_no_cache_in_db() -> None:
    print("\n=== 不落转码缓存 ===")
    lib, root = tmp_library()
    try:
        f = root / "v.mkv"
        touch(f)
        lib.scan()
        vid = video_id_for(f)
        lib.save_media_info(vid, type("I", (), {
            "duration": 12.5, "direct": False, "width": 640, "height": 360,
            "vcodec": "hevc", "acodec": "aac",
        })())
        info = lib.media_info(vid)
        check("媒体信息已缓存", info is not None and info["vcodec"] == "hevc")
        check("库里没有任何转码产物",
              not any(p.suffix in (".ts", ".m3u8") for p in root.rglob("*")),
              str([str(p) for p in root.rglob("*")]))

        # 文件一改, 重新扫描后缓存的探测结果要作废
        os.utime(f, (1, 1))
        lib.scan()
        check("文件改动后缓存作废", lib.media_info(vid) is None)
        check("改动后时长/编码需要重新探测", lib.get(vid).to_json()["probed"] is False)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    for fn in (test_wilson, test_labels, test_natural_sort, test_scan,
               test_rename_keeps_history, test_vote_semantics, test_sorting_modes,
               test_no_cache_in_db):
        fn()
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
