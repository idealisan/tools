# 视频收藏 Web 播放器

在电脑上跑一个小服务，用手机浏览器连上去，连续播放电脑里的视频，
顺手**滑动**记下喜欢 / 不喜欢。时间久了，程序就知道哪些视频是你的菜。

设计上只做四件事：**播放 → 上一个/下一个 → 喜欢/不喜欢 → 长期累积**。

---

## 手势

| 动作 | 效果 |
| --- | --- |
| ⬆️ 上滑 | 下一个视频 |
| ⬇️ 下滑 | 上一个视频 |
| ⬈ 左滑 | 喜欢 |
| ➡️ 右滑 | 不喜欢 |
| 轻点 | 播放 / 暂停 |
| 长按 | 撤销本设备最近一次标记 |

桌面上用方向键和空格、空格旁的 `U` 也能操作，方便调试。

## 跑起来

```bash
cd fav-marker
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 在视频所在目录启动
./.venv/bin/python app.py --videos ~/Movies
```

启动后终端会打印一个局域网地址，手机连同一个 Wi-Fi 打开就行：

```
手机浏览器打开:  http://192.168.1.208:5000
```

不写 `--videos` 就扫描**当前目录**。

### 常用参数

| 参数 | 说明 | 默认 |
| --- | --- | --- |
| `--videos` | 视频目录，可给多个 | 当前目录 |
| `--port` | 端口 | 5000 |
| `--db` | 累积记录存哪 | `data/fav.db` |
| `--max-height` | 转码输出最高高度 | 720 |
| `--crf` | libx264 画质，越小越清晰越慢 | 23 |
| `--encoder` | `auto` / `libx264` / `h264_videotoolbox` / `h264_nvenc` | `auto` |
| `--hls-cache-mb` | 转码分片在磁盘上最多留多少 MB | 100 |

也可以 `cp config.example.json config.json` 写死配置，命令行参数优先。

---

## 播不了的格式怎么办

mp4 / H.264 这类浏览器能直接播的，**直接送原文件**，不转码。

mkv、avi、wmv、hevc、ac3 之类浏览器播不了的，交给 **ffmpeg 边转边播**：
输出 HLS 分片，手机当直播流看。转码跟着播放走，边看边转。

**不留缓存**：分片只存在一个临时目录，客户端断开、3 分钟无操作、Ctrl-C 都会立刻收干净，
下次启动还会扫掉上次崩溃留下的残留。磁盘占用由 `--hls-cache-mb` 封顶（默认 100MB），
用完即删，不常驻。

### 进度条为什么不会跳回 0

转码会话每次都从 0 重新编号（这样 `-ss` 快 seek 才干净），所以播放器里的
`currentTime` 是"这条临时流播了多久"，不是"片子里第几秒"。服务端把偏移量
（`base`）一起告诉前端，进度条和时间标签都按 `base + currentTime` 换算回真实位置。

拖到还没转出来的位置时，服务端从那儿重开一条流，偏移量跟着变 ——
**进度条始终落在真实位置，不会归零**。

播放列表里也会写 `#EXT-X-START:TIME-OFFSET`，iOS 原生播放器靠它自己对齐。

### 磁盘上限怎么真的生效

转码比播放快好几倍，不管的话几秒钟就能把整部片子转完、磁盘爆掉。
服务端会盯着"播放点前方堆了多少"，超了就用 SIGSTOP 把 ffmpeg 挂起，
播放追上来再放行。所以 `--hls-cache-mb` 同时决定了两件事：

* 磁盘最多占多少
* 播放点前方留多少可跳范围（100MB @ 720p 约合 4~5 分钟）

剪枝只从播放点**前面**删 —— 这是关键。早期版本用 ffmpeg 自己的
`delete_segments`，它删的是"最新 N 片"，而转码跑在播放头前面，
等于把用户正在看的分片从磁盘上抹掉，超过 4 分钟的片子必中。

想跳更远就调大 `--hls-cache-mb`（比如 500），代价是磁盘和 CPU。

想指定临时目录（放到 SSD 上更快）：`export FAV_HLS_TMP=/fast/scratch`。

转码比播放快很多（1080p HEVC 实测约 7 倍实时），所以通常开头一两秒就出画面。
想省 CPU 可以把 `--max-height` 调低；`--encoder auto` 会优先用
VideoToolbox（macOS）/ NVENC（NVIDIA）硬件编码。

需要装 ffmpeg：

```bash
brew install ffmpeg          # macOS
sudo apt install ffmpeg      # Debian / Ubuntu
```

没装也能用，只是不能播 mkv / avi / hevc 这些格式。

### 已知的取舍

* 转码比播放快，但**磁盘封顶**决定了播放点前方只有约 4~5 分钟可跳
  （`--hls-cache-mb 100` @ 720p）。往更远处跳需要重开一次流，会有不到一秒的
  黑屏闪烁和音频断一下 —— 但进度条不会归零。调大 `--hls-cache-mb` 可以换取更远的
  可跳范围。
* 同时只转一个视频。快速连拖进度条会反复起停 ffmpeg（已做 220ms 防抖），
  不会互相干扰，也不会堆进程。
* 转码画质 720p，是给手机屏幕看的。要更清晰就 `--max-height 1080`
  （磁盘占用会按比例涨）。

---

## 喜欢程度怎么算

不用「赞 / (赞+踩)」，那个太容易被一次误操作带偏 —— 点错了喜欢一下，
一个从没看过的视频就 100% 好评排到最前面了。

用的是 **Wilson 评分区间下界**，样本少的时候自动往 0.5 拉：

| 记录 | 净好评率 | Wilson |
| --- | --- | --- |
| 1 赞 0 踩 | 100% | **0.21** |
| 30 赞 10 踩 | 75% | **0.60** |
| 100 赞 0 踩 | 100% | **0.96** |

所以「只喜欢过一次的」不会混到「一直很喜欢」里面去，
攒够记录之后排序才真的可信。

顶栏可以随时换看法：

* **最喜欢** — 按 Wilson 评分，长期最爱的排最前
* **喜欢最多** / **不喜欢最多** — 按次数
* **久未看** — 没标记和很久没碰的排前面，用来持续发现新片
* **未标记** — 只列出还没标记过的，专门用来把数据填满
* **按文件名** — 按目录顺序浏览

底部会直接给结论：未标记 / 喜欢 / 全喜欢 / 比较喜欢 / 最喜欢 / 不太喜欢。

---

## 数据

全部存在 `--db` 指的地方（默认 `data/fav.db`），一个 SQLite 文件。
**别删，删了记录就没了。**

* 每次左滑 / 右滑都是 **+1**，只增不减 —— 同一部剧反复看、反复喜欢就反复加分。
  想反悔就长按撤销。
* 撤销只影响**当前设备**的操作。手机和平板各记各的，总数是所有人加起来。
  每台设备在浏览器 localStorage 里认一个身份。
* 视频改名或挪了目录，只要大小和修改时间没变，重新扫描时会被认成同一个文件，
  累积的记录跟着走 —— 整理一次目录不会白记。
* 文件真被删了，它的视频条目会清掉，统计里也不计入。

加了新视频 / 删了视频，重启程序就会重新扫描。也可以随时调接口：

```bash
curl -X POST http://127.0.0.1:5000/api/rescan
```

---

## 目录结构

```
app.py            路由：页面、视频流、HLS 播放列表/分片、JSON 接口
library.py        扫描目录、SQLite、Wilson 评分、投票与撤销
media.py          ffprobe 探测能否直放、ffmpeg 直播转码与会话管理
templates/        页面
static/           样式、手势逻辑、随项目附带的 hls.js（离线也能用）
tests/            测试
```

### 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/library?sort=wilson` | 视频列表 + 累计数据 |
| GET | `/api/playback/<id>` | 这个视频直放还是转码、真实时长 |
| POST | `/api/vote` | `{id, action: like\|dislike}`，+1 |
| POST | `/api/undo` | `{id}`，撤销本设备最近一次 |
| POST | `/api/hls/start` / `/api/hls/stop` | 起停转码会话 |
| POST | `/api/rescan` | 重新扫描目录 |
| GET | `/api/stats` | 总计 |
| GET | `/api/media/status` | 当前转码会话状态 |

---

## 测试

```bash
# 单元测试（评分、排序、扫描、改名保留记录、投票语义）
./.venv/bin/python tests/test_library.py

# 接口测试，需要先起服务
MEDIA=/path/to/test/videos ./tests/with_server.sh tests/backend.py

# 浏览器端到端（手势、真实播放、ffmpeg 转码）
./.venv/bin/pip install playwright && ./.venv/bin/playwright install chromium
MEDIA=/path/to/test/videos ./tests/with_server.sh tests/e2e.py
./tests/with_server.sh tests/seek.py         # 跳转 / 进度条不回零
./tests/with_server.sh tests/autoplay.py     # 连播
./tests/with_server.sh tests/cap.py          # 磁盘上限 + 背压
./tests/with_server.sh tests/session_cap.py  # 并发上限
./tests/with_server.sh tests/shots.py        # 截图
SERVER_ARGS='--hls-cache-mb 8' ./tests/with_server.sh tests/segments.py   # 分片剪枝

# 转码临时目录不留残留（会自己起停服务）
MEDIA=/path/to/test/videos ./.venv/bin/python tests/leak_check.py
```

`tests/with_server.sh` 会起一个临时服务、跑完收干净，端口用 `PORT=xxxx` 指定，
媒体目录用 `MEDIA=` 指定，默认读 `/tmp/favtest/media`。

造测试素材：

```bash
mkdir -p /tmp/favtest/media && cd /tmp/favtest/media
ffmpeg -f lavfi -i testsrc=size=640x360:rate=25:duration=12 -f lavfi -i sine=duration=12 \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest direct.mp4 -y     # 直放
ffmpeg -f lavfi -i testsrc=size=640x360:rate=25:duration=12 -f lavfi -i sine=duration=12 \
  -c:v libx265 -pix_fmt yuv420p -c:a aac -shortest hevc.mkv -y       # 要转码
ffmpeg -f lavfi -i testsrc=size=640x360:rate=25:duration=8 \
  -c:v mpeg4 -an old.avi -y                                        # 没音轨的老格式
# 剪枝和磁盘上限要用长片子才测得出来
ffmpeg -f lavfi -i testsrc2=size=1920x1080:rate=30:duration=600 \
  -c:v libx265 -pix_fmt yuv420p -c:a aac -shortest long_hevc.mkv -y
```

`tests/hlsconfig.py` 是个诊断脚本：把 hls.js 几种配置都试一遍，
看哪种能从流的开头播而不是贴着末尾跳。改播放参数后值得跑一下。

---

## 说明

* 监听 `0.0.0.0`，同一个局域网内谁都能访问。放到公网前请自行加认证。
* 文件名和路径不会直接拼进请求：视频 ID 是路径的哈希，只服务扫描到、
  此刻还在的文件；HLS 分片名用固定正则校验，目录穿越回 404。
* 用的是 Flask 自带的开发服务器，自用足够；要在公网跑请换 gunicorn / waitress。
* 同一时间最多 4 个转码会话（正常只有 1 个），超了会返回 503 而不是把 CPU 跑满。
* 转码只在有人看的时候跑，切走立刻掐掉 ffmpeg 并清临时目录。
