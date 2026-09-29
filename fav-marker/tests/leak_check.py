"""转码临时目录不留残留 验收。

两件事:
  1. 收到 SIGTERM 时要收拾干净 (Ctrl-C / kill 走这条路)
  2. 上次进程被强杀留下的目录, 下次启动要能扫掉
"""

import glob
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

FAILS: list[str] = []
ROOT = Path("/tmp/favhls-test-tmp")
ROOT.mkdir(parents=True, exist_ok=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def dirs() -> list[str]:
    return glob.glob(str(ROOT / "favhls-*"))


def wait_health(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def start_transcode(port: int) -> bool:
    lib = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/library", timeout=20))
    need = [v["id"] for v in lib["videos"] if not v.get("direct")]
    if not need:
        return False
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/hls/start",
        data=json.dumps({"id": need[0]}).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Client-Id": "leak-test"},
    )
    urllib.request.urlopen(req, timeout=60).read()
    return True


def launch(port: int, video: str) -> subprocess.Popen:
    env = {**os.environ, "FAV_HLS_TMP": str(ROOT)}
    # 输出写文件而不是 PIPE: 没人读的管道写满会把服务端卡死
    log = open(f"/tmp/favhls-test-{port}.log", "w")
    return subprocess.Popen(
        [sys.executable, "app.py", "--videos", video, "--port", str(port),
         "--db", "/tmp/favtest/leak.db", "--hls-cache-mb", "40"],
        stdout=log, stderr=subprocess.STDOUT, env=env,
    )


def server_log(port: int) -> str:
    try:
        with open(f"/tmp/favhls-test-{port}.log") as fh:
            return fh.read()[-600:]
    except OSError:
        return "(没有日志)"


def main() -> int:
    video = os.environ.get("MEDIA", "/tmp/favtest/media")
    for d in dirs():
        subprocess.run(["rm", "-rf", d], check=False)

    print("\n=== 1. SIGTERM 要收拾干净 ===")
    proc = launch(5093, video)
    up = wait_health(5093)
    check("服务起来了", up, "" if up else "服务端日志:\n" + server_log(5093))
    if not up:
        proc.kill()
        raise SystemExit(1)
    check("转码已启动", start_transcode(5093))
    time.sleep(1.5)
    before = len(dirs())
    check("确实产生了临时目录", before > 0, f"{before} 个")

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=25)
    time.sleep(0.8)
    after = len(dirs())
    check("SIGTERM 之后没有残留目录", after == 0, f"还剩 {after} 个")
    ffmpeg_left = subprocess.run(
        ["pgrep", "-f", "ffmpeg.*" + str(ROOT)], capture_output=True).returncode == 0
    check("ffmpeg 进程也收掉了", not ffmpeg_left,
          "还有 ffmpeg 活着" if ffmpeg_left else "干净")

    print("\n=== 2. 上次强杀留下的目录要能扫掉 ===")
    stale = ROOT / "favhls-abcdef123456-deadbeef"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "seg00000.ts").write_bytes(b"x" * 1024)
    old = time.time() - 7200
    os.utime(stale, (old, old))                    # 装成两小时前的
    fresh = ROOT / "favhls-111111111111-222222222222"
    fresh.mkdir(parents=True, exist_ok=True)       # 刚建的, 不该动
    check("准备: 一个过期目录", stale.is_dir())

    proc = launch(5094, video)
    check("服务起来了", wait_health(5094))
    time.sleep(0.5)
    check("过期目录被扫掉", not stale.exists())
    check("较新的目录保留 (可能是别的实例在用)", fresh.exists())
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=25)
    time.sleep(0.5)

    for d in dirs():
        subprocess.run(["rm", "-rf", d], check=False)
    print("\n" + "=" * 44)
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
