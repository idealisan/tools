/* 视频收藏 —— 手势播放器
 *
 * 手势约定:
 *   上滑 = 下一个      下滑 = 上一个
 *   左滑 = 喜欢        右滑 = 不喜欢
 *   轻点 = 播放 / 暂停
 *   长按 = 撤销本设备最近一次标记
 *
 * 键盘 (桌面调试用): ↑↓ 切换, ←→ 标记, 空格 播放暂停, U 撤销
 */

'use strict';

const $ = (sel) => document.querySelector(sel);

const el = {
  stage: $('#stage'),
  video: $('#player'),
  overlay: $('#overlay'),
  ovIcon: $('#overlay .ov-icon'),
  ovLabel: $('#overlay .ov-label'),
  centerPlay: $('#centerPlay'),
  loading: $('#loading'),
  loadingText: $('#loadingText'),
  hint: $('#hint'),
  hintClose: $('#hintClose'),
  pos: $('#pos'),
  sortBtn: $('#sortBtn'),
  sortMenu: $('#sortMenu'),
  autoBtn: $('#autoBtn'),
  fsBtn: $('#fsBtn'),
  title: $('#title'),
  cLike: $('#cLike'),
  cDislike: $('#cDislike'),
  cLabel: $('#cLabel'),
  cWilson: $('#cWilson'),
  progress: $('#bar-progress'),
  fill: $('#bar-fill'),
  ghost: $('#bar-ghost'),
  toast: $('#toast'),
};

const SORTS = [
  ['wilson', '最喜欢', 'Wilson 评分, 长期最爱的排最前'],
  ['likes', '喜欢最多', '按喜欢次数'],
  ['recent', '久未看', '没标记和很久没碰的排前面, 持续发现新片'],
  ['unvoted', '未标记', '只列出还没标记过的视频'],
  ['folder', '按文件名', '按目录顺序浏览'],
  ['disliked', '不喜欢最多', '按不喜欢次数'],
];

const DIRECTIONS = {
  up:    { icon: '⬆️', label: '下一个' },
  down:  { icon: '⬇️', label: '上一个' },
  left:  { icon: '❤️', label: '喜欢' },
  right: { icon: '👎', label: '不喜欢' },
};

const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem('fav.' + key); return v === null ? fallback : JSON.parse(v); }
    catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem('fav.' + key, JSON.stringify(value)); } catch { /* 隐私模式下忽略 */ }
  },
};

const state = {
  clientId: store.get('cid', null) || (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2)),
  sort: store.get('sort', window.DEFAULTS.sort || 'folder'),
  autoplay: store.get('autoplay', !!window.DEFAULTS.autoplay),
  videos: [],
  index: 0,
  sid: null,        // 当前 HLS 转码会话
  hls: null,        // hls.js 实例 (桌面浏览器用)
  playlist: null,   // 当前 HLS 播放列表地址
  ffmpeg: true,     // 服务端能不能转码, 来自 /api/library
  offset: 0,        // 本地 0 秒对应的全局时间。转码会话从 -ss T 起播时就是 T
  whereUrl: null,   // 问服务端"现在能跳到哪儿"
  loadToken: 0,     // 防止快速切换时旧请求覆盖新状态
};

/* 全局时间 = 本会话的本地时间 + 偏移。
 * 转码会话每次都从 0 重新编号 (为了保证 -ss 快 seek 干净),
 * 所以进度条、时间标签全靠这个换算回片子里真实的位置。 */
const globalNow = () => state.offset + (el.video.currentTime || 0);

store.set('cid', state.clientId);

/* ------------------------------------------------------------------ 网络 */

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      'X-Client-Id': state.clientId,
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(options.headers || {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).error || msg; } catch { /* 忽略 */ }
    throw new Error(msg);
  }
  return res.json();
}

/* ------------------------------------------------------------------ 提示 */

let toastTimer = 0;
function toast(text, kind = '') {
  el.toast.textContent = text;
  el.toast.className = 'toast' + (kind ? ' ' + kind : '');
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.toast.hidden = true; }, 1600);
}

function buzz(ms) {
  if (navigator.vibrate) { try { navigator.vibrate(ms); } catch { /* 不支持就算了 */ } }
}

const fmtTime = (s) => {
  if (!isFinite(s) || s < 0) return '0:00';
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
           : `${m}:${String(sec).padStart(2, '0')}`;
};

/* ------------------------------------------------------------------ 手势
 *
 * 用 Pointer Events 而不是 Touch Events: 手机、桌面鼠标、触控笔走同一套代码,
 * 桌面调试时也能用鼠标拖出同样的手势。
 */

let gesture = null;
let longTimer = 0;
const THRESHOLD = () => Math.max(56, Math.min(104, Math.round(Math.min(innerWidth, innerHeight) * 0.16)));

function directionOf(dx, dy) {
  return Math.abs(dx) > Math.abs(dy)
    ? (dx < 0 ? 'left' : 'right')
    : (dy < 0 ? 'up' : 'down');
}

function showOverlay(dir, strength) {
  const d = DIRECTIONS[dir];
  el.ovIcon.textContent = d.icon;
  el.ovLabel.textContent = d.label;
  el.overlay.style.opacity = Math.min(1, 0.35 + strength * 0.65).toFixed(2);
  el.overlay.classList.add('on');
}

function hideOverlay() {
  el.overlay.classList.remove('on');
  el.overlay.style.opacity = '';
}

function flash(dir) {
  const d = DIRECTIONS[dir];
  el.ovIcon.textContent = d.icon;
  el.ovLabel.textContent = d.label;
  el.overlay.style.opacity = '1';
  el.overlay.classList.add('on');
  setTimeout(hideOverlay, 420);
}

el.stage.addEventListener('pointerdown', (e) => {
  if (gesture) return;                        // 已经在跟踪一根手指了
  // 说明面板/按钮上的按下不当作手势, 否则 setPointerCapture 会把 click 吃掉
  if (e.target.closest('button, .hint, .sort-menu, a')) return;
  gesture = { id: e.pointerId, x: e.clientX, y: e.clientY, moved: false, fired: false };
  el.stage.setPointerCapture(e.pointerId);
  clearTimeout(longTimer);
  longTimer = setTimeout(() => {
    if (gesture && !gesture.moved) { gesture.fired = true; doUndo(); }
  }, 550);
});

el.stage.addEventListener('pointermove', (e) => {
  if (!gesture || e.pointerId !== gesture.id) return;
  const dx = e.clientX - gesture.x;
  const dy = e.clientY - gesture.y;
  const dist = Math.hypot(dx, dy);
  if (!gesture.moved && dist > 12) {
    gesture.moved = true;
    clearTimeout(longTimer);
  }
  if (gesture.moved && dist > 20) {
    e.preventDefault();
    showOverlay(directionOf(dx, dy), dist / THRESHOLD());
  }
});

el.stage.addEventListener('pointerup', (e) => {
  if (!gesture || e.pointerId !== gesture.id) return;
  const g = gesture;
  gesture = null;
  clearTimeout(longTimer);
  hideOverlay();
  if (el.stage.hasPointerCapture(e.pointerId)) el.stage.releasePointerCapture(e.pointerId);

  if (g.fired) return;
  if (!g.moved) { togglePlay(); return; }

  const dx = e.clientX - g.x;
  const dy = e.clientY - g.y;
  if (Math.hypot(dx, dy) < THRESHOLD()) return;
  act(directionOf(dx, dy));
});

el.stage.addEventListener('pointercancel', () => { gesture = null; clearTimeout(longTimer); hideOverlay(); });
el.stage.addEventListener('contextmenu', (e) => e.preventDefault());

/* 桌面端调试用 */
document.addEventListener('keydown', (e) => {
  const map = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right' };
  if (map[e.key]) { e.preventDefault(); act(map[e.key]); }
  else if (e.key === ' ') { e.preventDefault(); togglePlay(); }
  else if (e.key === 'u' || e.key === 'U') doUndo();
});

function act(dir) {
  buzz(12);
  flash(dir);
  if (dir === 'up') step(1);
  else if (dir === 'down') step(-1);
  else vote(dir === 'left' ? 'like' : 'dislike');
}

/* ------------------------------------------------------------------ 播放 */

function togglePlay() {
  if (el.video.paused) el.video.play().catch(() => {});
  else el.video.pause();
}

/* hls.js 挂 MediaSource 是异步的, 数据没就位就调 play() 会被浏览器拒绝 */
function waitReady(timeout = 25000) {
  return new Promise((resolve) => {
    if (el.video.readyState >= 1) { resolve(); return; }
    const done = () => {
      clearTimeout(timer);
      el.video.removeEventListener('loadedmetadata', done);
      el.video.removeEventListener('canplay', done);
      resolve();
    };
    const timer = setTimeout(done, timeout);
    el.video.addEventListener('loadedmetadata', done, { once: true });
    el.video.addEventListener('canplay', done, { once: true });
  });
}

el.video.addEventListener('play', () => { el.centerPlay.hidden = true; });
el.video.addEventListener('pause', () => { el.centerPlay.hidden = false; });
el.video.addEventListener('loadedmetadata', () => {
  // 直播 HLS 的 video.duration 只是"目前已经转出来"的那一段,
  // 直接采纳会把 180 秒的片子显示成 4 秒 —— 直放时才用它覆盖
  if (el.video.dataset.mode === 'direct'
      && isFinite(el.video.duration) && el.video.duration > 0) {
    state.duration = el.video.duration;
  }
  paintProgress();
});

el.video.addEventListener('timeupdate', paintProgress);
el.video.addEventListener('ended', () => {
  // 直播 HLS 的播放列表会一直更新, 用缓冲区判断是否真的看完了
  if (el.video.buffered.length && el.video.currentTime + 1 < el.video.buffered.end(el.video.buffered.length - 1)) {
    return;
  }
  if (state.autoplay) step(1);
});

el.video.addEventListener('error', () => {
  const v = current();
  if (!v) return;
  // ffprobe 说能直放但浏览器还是放不了 (比如 mp4 里的 hevc), 退回到转码
  if (el.video.dataset.mode === 'direct') {
    toast('该格式无法直接播放, 正在转码…');
    el.video.dataset.mode = 'hls';
    showLoading(true, '转码中…');
    startHlsWithFallback(v, 0)
      .then(() => el.video.play().catch(() => {}))
      .catch((err) => { showLoading(false); toast(err.message || '转码失败'); });
    return;
  }
  showLoading(false);
  toast('这个视频播不了, 换一个');
  buzz(40);
  if (state.autoplay) step(1);
});

function paintProgress() {
  const dur = state.duration || el.video.duration || 0;
  const cur = globalNow();
  el.fill.style.width = dur > 0 ? `${Math.max(0, Math.min(100, (cur / dur) * 100))}%` : '0%';
  if (dur > 0) el.cWilson.textContent = fmtTime(cur) + ' / ' + fmtTime(dur);
}

/* 拖动过程中只更新"想去哪", 不动播放器 —— 免得一路拖触发十几次重开流。 */
let seekTarget = 0;
let scrubbing = false;
let seekTimer = 0;
function previewSeek(e) {
  const dur = state.duration || el.video.duration || 0;
  if (dur <= 0) return;
  const box = el.progress.getBoundingClientRect();
  const x = (e.clientX - box.left - 14) / Math.max(1, box.width - 28);
  seekTarget = Math.max(0, Math.min(1, x)) * dur;
  el.ghost.style.width = `${Math.min(100, (seekTarget / dur) * 100)}%`;
  el.cWilson.textContent = fmtTime(seekTarget) + ' / ' + fmtTime(dur);
}

/* 松手才真的跳: 已经转出来的直接跳, 没转出来的让服务端从那儿重新起转码。 */
async function commitSeek() {
  const v = current();
  const dur = state.duration || el.video.duration || 0;
  if (!v || dur <= 0) return;
  const target = seekTarget;
  el.ghost.style.width = '0%';

  if (el.video.dataset.mode !== 'hls') {          // 原文件: 想跳哪跳哪
    el.video.currentTime = Math.min(target, dur);
    return;
  }

  // 问服务端现在能到哪儿 (会话是活的, 边转边变)
  let w = null;
  if (state.whereUrl) {
    try { w = await api(state.whereUrl); } catch { w = null; }
  }
  if (!w) { toast('问一下能跳到哪儿失败了, 试试原地播放'); return; }

  if (target >= w.available_start - 0.5 && target <= w.available_end + 0.5) {
    el.video.currentTime = Math.max(0, target - state.offset);
    return;
  }
  // 超出已转出的范围: 从那儿重开一条流。offset 跟着变, 进度条不会归零
  showLoading(true, '跳转中…');
  try {
    await teardownHls();
    el.ghost.style.width = '0%';
    await startHlsWithFallback(v, target);
    await waitReady();
    showLoading(false);
    await el.video.play().catch(() => {});
    toast(`已跳到 ${fmtTime(target)}`, '');
  } catch (err) {
    showLoading(false);
    toast(err.message || '跳转失败');
  }
}

el.progress.addEventListener('pointerdown', (e) => {
  scrubbing = true;
  el.progress.classList.add('dragging');
  el.progress.setPointerCapture(e.pointerId);
  previewSeek(e);
});
el.progress.addEventListener('pointermove', (e) => { if (scrubbing) previewSeek(e); });
el.progress.addEventListener('pointerup', (e) => {
  if (!scrubbing) return;
  scrubbing = false;
  el.progress.classList.remove('dragging');
  previewSeek(e);
  if (el.progress.hasPointerCapture(e.pointerId)) el.progress.releasePointerCapture(e.pointerId);
  // 防抖: 松手瞬间偶尔会连着来两个 pointerup, 只处理最后一个
  clearTimeout(seekTimer);
  seekTimer = setTimeout(commitSeek, 220);
});

/* ------------------------------------------------------------------ 切片 */

const current = () => state.videos[state.index] || null;

function step(delta) {
  if (!state.videos.length) return;
  const next = state.index + delta;
  if (next < 0) { toast('已经是第一个了'); return; }
  if (next >= state.videos.length) { toast('已经是最后一个了'); return; }
  state.index = next;
  store.set('pos', current().id);
  load();
}

async function vote(action) {
  const v = current();
  if (!v) return;
  try {
    const r = await api('/api/vote', { method: 'POST', body: { id: v.id, action } });
    Object.assign(v, r.video);              // 就地更新, 保持当前顺序不变
    paintVideo();
    toast(action === 'like' ? `已喜欢 · 共 ${v.like} 次` : `已标记不喜欢 · 共 ${v.dislike} 次`, action);
  } catch (err) {
    toast('记录失败: ' + err.message);
  }
}

async function doUndo() {
  const v = current();
  if (!v) return;
  if (!v.my_like && !v.my_dislike) { toast('这个视频还没有你的标记'); return; }
  try {
    const r = await api('/api/undo', { method: 'POST', body: { id: v.id } });
    if (!r.undone) { toast('没有可撤销的操作'); return; }
    Object.assign(v, r.video);
    paintVideo();
    toast('已撤销上一次标记');
  } catch (err) {
    toast('撤销失败: ' + err.message);
  }
}

function paintVideo() {
  const v = current();
  if (!v) {
    el.title.textContent = state.videos.length ? '' : '没有找到视频';
    el.pos.textContent = '0 / 0';
    return;
  }
  el.pos.textContent = `${state.index + 1} / ${state.videos.length}`;
  el.title.textContent = v.title;
  el.title.title = v.rel_path;
  el.cLike.textContent = v.like;
  el.cDislike.textContent = v.dislike;
  el.cLabel.textContent = v.label;
  el.fill.style.width = '0%';
  el.cWilson.textContent = fmtTime(0) + ' / ' + fmtTime(state.duration || 0);
  document.title = v.title;
}

/* ------------------------------------------------------------------ 装载 */

async function teardownHls() {
  el.loading.hidden = true;
  if (state.hls) { try { state.hls.destroy(); } catch { /* 忽略 */ } state.hls = null; }
  const old = state.sid;
  state.sid = null;
  state.playlist = null;
  state.whereUrl = null;
  state.offset = 0;
  el.video.removeAttribute('src');
  el.video.load();
  if (!old) return;
  // 必须等后端真的掐掉 ffmpeg 再返回: 同时只允许一个转码会话,
  // 不等的话紧接着发起的 /api/hls/start 会撞上 503。
  try {
    await api('/api/hls/stop', { method: 'POST', body: { sid: old } });
  } catch { /* 服务端自己也会超时回收, 这里忽略 */ }
}

/* iOS / iPadOS 走原生 HLS (顺滑、无额外依赖), 其他一律交给 hls.js。
 * iPadOS 13+ 会把 UA 伪装成 Macintosh, 但真正的桌面 Chrome 也会命中同样的条件,
 * 所以额外排除掉 Chromium 系 —— 否则桌面机会走进一条它根本播不了的原生分支。 */
const useNativeHls = () => {
  const ua = navigator.userAgent;
  if (/iPad|iPhone|iPod/.test(ua)) return true;
  const iPadOS = /Macintosh/.test(ua) && navigator.maxTouchPoints > 1;
  return iPadOS && !/Chrome|Chromium|Edg|CriOS|FxiOS|OPiOS/i.test(ua);
};

function attachHls(playlist) {
  if (window.Hls && window.Hls.isSupported() && !useNativeHls()) {
    const hls = new window.Hls({
      // startPosition: 0 必需。播放列表是"一直变长"的流, 不指定的话
      // hls.js 会贴着流的末尾播 —— 转码比播放快好几倍, 开头会被整段跳掉。
      startPosition: 0,
      liveSyncDurationCount: 0,
      maxBufferLength: 12,
      enableWorker: true,
    });
    hls.on(window.Hls.Events.ERROR, (_e, data) => {
      if (data.fatal) console.warn('hls 错误', data.type, data.details);
    });
    hls.loadSource(playlist);
    hls.attachMedia(el.video);
    state.hls = hls;
    return hls;
  }
  el.video.src = playlist;           // iOS Safari / Android 原生
  return null;
}

/* 503 只表示"上一个会话还没让出名额", 是暂时性的, 退一下重试就好。
 * 不重试的话用户看到的就是一片黑屏加一句"转码任务太多"。 */
async function startHls(v, startAt, attempt = 0) {
  let r;
  try {
    r = await api('/api/hls/start', { method: 'POST', body: { id: v.id, start: startAt || 0 } });
  } catch (err) {
    if (attempt < 3 && /503|太多/.test(err.message)) {
      await new Promise((r2) => setTimeout(r2, 400 * (attempt + 1)));
      return startHls(v, startAt, attempt + 1);
    }
    throw err;
  }
  state.sid = r.sid;
  state.playlist = r.playlist;
  state.whereUrl = r.where || null;
  state.offset = r.base || 0;          // 本地 0 秒 = 全局 base 秒
  if (r.duration) state.duration = r.duration;
  return attachHls(r.playlist);
}

/* 兜底: 万一 UA 判断失手选了原生 HLS, 而这个浏览器其实播不了,
 * 等几秒没反应就换 hls.js 重来一次, 总比卡在黑屏好。 */
async function startHlsWithFallback(v, startAt) {
  const used = await startHls(v, startAt);
  if (used) return;                          // 已经走了 hls.js
  const ok = await Promise.race([
    new Promise((r) => el.video.addEventListener('loadedmetadata', () => r(true), { once: true })),
    new Promise((r) => setTimeout(() => r(false), 6000)),
  ]);
  if (ok || !state.playlist || !window.Hls || !window.Hls.isSupported()) return;
  console.warn('原生 HLS 没反应, 改用 hls.js');
  if (state.hls) { try { state.hls.destroy(); } catch { /* 忽略 */ } }
  el.video.removeAttribute('src');
  attachHls(state.playlist);
}

async function load() {
  const v = current();
  if (!v) return;
  const token = ++state.loadToken;

  await teardownHls();
  state.duration = v.duration || 0;
  paintVideo();

  // 直放几乎立刻就绪, 只有走转码才需要等第一个分片
  let transcoding = false;

  try {
    let info = v;
    if (!v.probed) {
      info = await api(`/api/playback/${v.id}`);
      Object.assign(v, { direct: info.direct, duration: info.duration || 0, probed: true });
      if (token !== state.loadToken) return;         // 用户已经滑走了
    }
    state.duration = info.duration || 0;
    paintVideo();

    el.video.dataset.mode = info.direct ? 'direct' : 'hls';
    if (info.direct) {
      state.offset = 0;             // 原文件的时间轴就是全局时间
      el.video.src = v.stream_url;
    } else {
      if (!state.ffmpeg) throw new Error('服务端没有 ffmpeg, 无法转码这个格式');
      transcoding = true;
      showLoading(true, '转码中…');
      await startHlsWithFallback(v, 0);
    }
    if (token !== state.loadToken) return;
    await waitReady();
    if (token !== state.loadToken) return;
    showLoading(false);
    await el.video.play().catch(() => { el.centerPlay.hidden = false; });
  } catch (err) {
    if (token !== state.loadToken) return;
    showLoading(false);
    toast(err.message || '播放失败');
    buzz(40);
  }
}

function showLoading(on, text) {
  el.loadingText.textContent = text || '转码中…';
  el.loading.hidden = !on;
}

async function loadLibrary() {
  const data = await api(`/api/library?sort=${encodeURIComponent(state.sort)}`);
  state.videos = data.videos;
  state.ffmpeg = data.ffmpeg !== false;
  if (!state.videos.length) { paintVideo(); return; }

  const saved = store.get('pos', null);
  const at = data.videos.findIndex((v) => v.id === saved);
  state.index = at >= 0 ? at : 0;
  paintVideo();
  load();
}

/* ------------------------------------------------------------------ 顶栏 */

function syncChrome() {
  const name = (SORTS.find((s) => s[0] === state.sort) || [])[1] || '排序';
  el.sortBtn.textContent = name;
  el.autoBtn.textContent = state.autoplay ? '连播 开' : '连播 关';
  el.autoBtn.setAttribute('aria-pressed', String(state.autoplay));
  document.body.dataset.sort = state.sort;
}

el.sortBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  const open = el.sortMenu.hidden;
  el.sortMenu.hidden = !open;
  if (!open) return;
  el.sortMenu.innerHTML = '';
  for (const [key, label, desc] of SORTS) {
    const b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('aria-current', String(key === state.sort));
    const name = document.createElement('span');
    name.className = 'sm-name';
    name.textContent = label;
    const hint = document.createElement('span');
    hint.className = 'sm-desc';
    hint.textContent = desc;
    b.append(name, hint);
    b.addEventListener('click', () => {
      el.sortMenu.hidden = true;
      if (key === state.sort) return;
      state.sort = key;
      store.set('sort', key);
      syncChrome();
      loadLibrary();
    });
    el.sortMenu.appendChild(b);
  }
});

document.addEventListener('click', () => { el.sortMenu.hidden = true; });

el.autoBtn.addEventListener('click', () => {
  state.autoplay = !state.autoplay;
  store.set('autoplay', state.autoplay);
  syncChrome();
  toast(state.autoplay ? '播完自动下一个' : '已关闭自动下一个');
  // 当前这个已经看完了才打开连播的话, ended 不会再触发, 得主动往前走
  if (state.autoplay && el.video.ended) step(1);
});

el.fsBtn.addEventListener('click', () => {
  const target = document.documentElement;
  if (document.fullscreenElement) document.exitFullscreen?.();
  else target.requestFullscreen?.().catch(() => toast('无法进入全屏'));
});

el.hintClose.addEventListener('click', () => {
  el.hint.hidden = true;
  store.set('hint', true);
});

window.addEventListener('pagehide', () => { if (state.sid) teardownHls(); });

/* ------------------------------------------------------------------ 启动 */

async function boot() {
  syncChrome();
  if (store.get('hint', false)) el.hint.hidden = true;
  try {
    await loadLibrary();
  } catch (err) {
    el.title.textContent = '连不上服务器: ' + err.message;
  }
}

boot();
