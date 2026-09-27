// core.js —— 全局状态、工具函数、SVG 图标表（window 命名空间基座）

// ---------------- 全局状态 ----------------
Object.assign(window.XQ.state, {
  view: 'work',
  segs: [],            // [{startFrame,endFrame,prompt,images:[assetKey],refVideos:[],refAudios:[]}]
  activeSeg: 0,
  // 自动首尾帧续写（段间引导）：null=跟随段数自动（多段开 / 单段关），
  // true / false 为用户手动覆盖后的固定值
  autoCont: null,
  assets: [],          // materials 桶清单 [{key,size,url,created_ms}]
  models: {},          // kind → [name...]
  engine: {},          // 引擎状态（/api/system/status 的 engine 字段）
  resultsKind: 'videos',
  resultsSel: new Set(),
  fps: () => {         // 当前模式参数集的帧率（参数状态在 workbench.js）
    const W = window.Workbench;
    const p = W && W.P ? W.P[window.XQ.state.genMode === 'cloud' ? 'cloud' : 'local']
                       : null;
    return Math.max(1, parseFloat(p && p.fps) || 24);
  },
});

// ---------------- DOM / 格式化工具 ----------------
export function el(id) { return document.getElementById(id); }

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function fmtBytes(n) {
  n = +n || 0;
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(2) + ' GB';
}

export function fmtTime(ms) {
  const d = new Date(+ms || 0);
  if (!d.getTime()) return '–';
  const p = x => String(x).padStart(2, '0');
  return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 帧区间显示规范：统一「起始-结束帧」（如 0-125帧），不再用 f 后缀 */
export function fmtFrames(a, b) {
  return `${Math.round(+a || 0)}-${Math.round(+b || 0)}帧`;
}

/** 秒数显示规范：统一「N秒」（取整），不再用 s 后缀 */
export function fmtSec(s) {
  return Math.max(0, Math.round(+s || 0)) + '秒';
}

export function fmtClock(ms) {
  const d = new Date(+ms || 0);
  const p = x => String(x).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function fmtDur(sec) {
  sec = +sec || 0;
  return sec >= 100 ? Math.round(sec) + 's' : sec.toFixed(1) + 's';
}

export function debounce(fn, wait) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), wait);
  };
}

export function extOf(key) { return (String(key).split('.').pop() || '').toLowerCase(); }

export const VIDEO_EXT = ['mp4', 'webm', 'mkv', 'mov', 'avi', 'm4v', 'ts'];
export const IMAGE_EXT = ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'];
export const AUDIO_EXT = ['wav', 'mp3', 'flac', 'ogg', 'aac', 'm4a', 'opus'];

// ---------------- SVG 图标表 ----------------
const ICONS = {
  clapper: ['M3.5 9.5h17v7.6a2.9 2.9 0 0 1-2.9 2.9H6.4a2.9 2.9 0 0 1-2.9-2.9V9.5z',
    'm3.8 9.4 1.5-4.3a2 2 0 0 1 2.4-1.3l10.9 2a2 2 0 0 1 1.6 2.3l-.5 1.5',
    'm8.7 4.9-1 4.4', 'm13.9 5.9-1 4.4'],
  chart: ['M22 12h-4l-3 9L9 3l-3 9H2'],
  stack: ['M12 2 2 7l10 5 10-5-10-5z', 'm2 17 10 5 10-5', 'm2 12 10 5 10-5'],
  gear: ['M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z',
    'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z'],
  save: ['M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z',
    'M17 21v-8H7v8', 'M7 3v5h8'],
  folder: ['M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z'],
  frames: ['M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5z',
    'M7 3v18', 'M17 3v18', 'M3 10h4', 'M17 10h4', 'M3 15h4', 'M17 15h4'],
  plus: ['M12 5v14', 'M5 12h14'],
  text: ['M4 7V4h16v3', 'M9 20h6', 'M12 4v16'],
  trash: ['M3 6h18', 'M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2',
    'M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6', 'M10 11v6', 'M14 11v6'],
  bolt: ['M13 2 3 14h9l-1 8 10-12h-9l1-8z'],
  film: ['M7 3v18', 'M17 3v18', 'M3 7.5h4', 'M3 12h18', 'M3 16.5h4',
    'M17 7.5h4', 'M17 16.5h4',
    'M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5z'],
  photo: ['M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5z',
    'M8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z', 'm21 15-5-5L5 21'],
  check: ['M20 6 9 17l-5-5'],
  square: ['M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z'],
  code: ['m16 18 6-6-6-6', 'm8 6-6 6 6 6'],
  x: ['M18 6 6 18', 'm6 6 12 12'],
  upload: ['M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4', 'm17 8-5-5-5 5', 'M12 3v12'],
  refresh: ['M23 4v6h-6', 'M1 20v-6h6',
    'M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15'],
  play: ['m5 3 14 9-14 9V3z'],
  pause: ['M6 4h4v16H6z', 'M14 4h4v16h-4z'],
  stop: ['M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z'],
  retry: ['M1 4v6h6', 'M3.51 15a9 9 0 1 0 2.13-9.36L1 10'],
  music: ['M9 18V5l12-2v13', 'M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0z',
    'M21 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z'],
  video: ['m23 7-7 5 7 5V7z', 'M14 5H3a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2z'],
  chevD: ['m6 9 6 6 6-6'],
  cloud: ['M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z'],
  user: ['M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2',
    'M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z'],
  scene: ['m8 3 4 8 5-5 5 15H2L8 3z'],
  prop: ['M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z',
    'm3.3 7 8.7 5 8.7-5', 'M12 22V12'],
  eye: ['M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z', 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z'],
};

export function icon(name, size = 14) {
  const ds = ICONS[name] || ICONS.square;
  const paths = ds.map(d => `<path d="${d}"/>`).join('');
  return `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 24 24" ` +
    `fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" ` +
    `stroke-linejoin="round">${paths}</svg>`;
}

export function renderIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach(n => {
    n.innerHTML = icon(n.dataset.icon, +(n.dataset.size || 14));
  });
}

// ---------------- toast 浮动提示 ----------------
export function toast(msg, kind = '') {
  let box = document.getElementById('toasts');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toasts';
    document.body.appendChild(box);
  }
  const t = document.createElement('div');
  t.className = 'toast ' + kind;
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(() => {
    t.classList.add('out');
    setTimeout(() => t.remove(), 350);
  }, kind === 'err' ? 6000 : 2800);
}

// 供经典 onclick 使用的错误兜底
window.addEventListener('error', e => {
  if (e.message) console.error('[XQ]', e.message);
});
