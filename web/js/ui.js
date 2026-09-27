// ui.js —— 视图导航 / 侧栏 / 弹窗 / 顶部徽标与总进度（window.UI）

import { el, esc, icon, renderIcons } from './core.js';
import { API } from './api.js';

const VIEWS = ['home', 'work', 'tasks', 'lib', 'json'];
const VID = { home: 'vHome', work: 'vWork', tasks: 'vTasks', lib: 'vLib', json: 'vJson' };
const LAST_VIEW_KEY = 'xqwui.lastView';
const STATE_TXT = {
  queued: '排队中', running: '生成中', pausing: '暂停中…', paused: '已暂停',
  canceling: '取消中…', canceled: '已取消', done: '已完成', error: '失败',
  merging: '合并成片中',
};

export const UI = {
  /** 顶部视图切换 */
  nav(view, btn) {
    const S = window.XQ.state;
    S.view = view;
    document.body.classList.toggle('in-work', view === 'work');
    VIEWS.forEach(v => el(VID[v]).classList.toggle('hide', v !== view));
    // 底部悬浮时间轴仅在工作台显示（其他页面不需要）
    el('tlDock').classList.toggle('hide', view !== 'work');
    document.querySelectorAll('.nav button').forEach(b =>
      b.classList.toggle('on', b === btn || b.dataset.v === view));
    // 路由同步：hash 记录当前视图（replaceState 不产生历史记录），刷新后原位恢复
    if (location.hash !== '#/' + view)
      history.replaceState(null, '', '#/' + view);
    try { localStorage.setItem(LAST_VIEW_KEY, view); } catch { /* 隐私模式忽略 */ }
    if (view === 'home') window.Projects && Projects.render();
    if (view === 'tasks') window.Tasks && Tasks.reload();
    if (view === 'lib') window.Results && Results.reload();
    if (view === 'json') window.Workbench && Workbench.preview(true);
  },

  /** 从 URL hash 解析视图（缺失或非法返回 null，由调用方决定默认落点） */
  viewFromHash() {
    const v = location.hash.replace(/^#\/?/, '');
    return VIEWS.includes(v) ? v : null;
  },

  /** 上次停留的视图（无记录或非法返回 null） */
  lastView() {
    try {
      const v = localStorage.getItem(LAST_VIEW_KEY);
      return VIEWS.includes(v) ? v : null;
    } catch { return null; }
  },

  /** 打开任务中心（header 快捷按钮） */
  openTasks() {
    const btn = document.querySelector('.nav button[data-v="tasks"]');
    UI.nav('tasks', btn);
  },

  // ---------------- 弹窗 ----------------
  /** 在 .modal 容器里渲染 box 内容并显示；返回关闭函数。
      opts.onResizeEnd(box, {dir, width, height}) —— 拖拽缩放结束后回调；
      结束时同时在 box 上派发冒泡的 'boxresize' 事件 */
  showModal(id, html, { wide = false, onResizeEnd } = {}) {
    const m = el(id);
    const handles = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw']
      .map(d => `<span class="rz-h" data-dir="${d}"></span>`).join('');
    m.innerHTML = `<div class="box${wide ? ' w760' : ''}">
      <button class="mclose" title="关闭">${icon('x', 15)}</button>${html}${handles}</div>`;
    m.classList.add('on');
    renderIcons(m);
    m.querySelector('.mclose').onclick = () => UI.closeModal(id);
    m.onclick = (e) => { if (e.target === m) UI.closeModal(id); };
    UI._resizable(m.querySelector('.box'), id, onResizeEnd);
    return () => UI.closeModal(id);
  },

  /** 弹窗 box 拖拽缩放：8 方向手柄，最小尺寸 + 视口边界约束，
      拖拽中固定定位防布局跳动，结束后触发回调与 'boxresize' 事件 */
  _resizable(box, id, onResizeEnd) {
    if (!box) return;
    const MIN_W = 320, MIN_H = 200, PAD = 8;   // 最小宽高与视口留白
    box.addEventListener('pointerdown', e => {
      const h = e.target.closest('.rz-h');
      if (!h) return;
      e.preventDefault();
      const dir = h.dataset.dir;
      const r = box.getBoundingClientRect();
      const vw = window.innerWidth, vh = window.innerHeight;
      // 以当前矩形为基准切换为固定定位，拖拽期间布局零漂移
      Object.assign(box.style, {
        position: 'fixed', left: r.left + 'px', top: r.top + 'px',
        width: r.width + 'px', height: r.height + 'px',
        maxWidth: 'none', maxHeight: 'none',
      });
      box.classList.add('resizing');
      const sx = e.clientX, sy = e.clientY;
      const move = ev => {
        const dx = ev.clientX - sx, dy = ev.clientY - sy;
        let L = r.left, T = r.top, W = r.width, H = r.height;
        if (dir.includes('e')) W = Math.min(r.width + dx, vw - PAD - L);
        if (dir.includes('s')) H = Math.min(r.height + dy, vh - PAD - T);
        if (dir.includes('w')) {
          L = Math.max(PAD, Math.min(L + dx, r.right - MIN_W));
          W = r.right - L;
        }
        if (dir.includes('n')) {
          T = Math.max(PAD, Math.min(T + dy, r.bottom - MIN_H));
          H = r.bottom - T;
        }
        W = Math.max(MIN_W, Math.min(W, vw - PAD * 2));
        H = Math.max(MIN_H, Math.min(H, vh - PAD * 2));
        box.style.left = L + 'px';
        box.style.top = T + 'px';
        box.style.width = W + 'px';
        box.style.height = H + 'px';
      };
      const up = () => {
        document.removeEventListener('pointermove', move);
        document.removeEventListener('pointerup', up);
        document.removeEventListener('pointercancel', up);
        box.classList.remove('resizing');
        const size = { width: box.offsetWidth, height: box.offsetHeight };
        const detail = { id, dir, ...size };
        if (onResizeEnd) onResizeEnd(box, detail);
        box.dispatchEvent(new CustomEvent('boxresize',
          { bubbles: true, detail }));
      };
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', up);
      document.addEventListener('pointercancel', up);
    });
  },

  closeModal(id) {
    const m = el(id);
    m.classList.remove('on');
    m.innerHTML = '';
  },

  // ---------------- 顶部徽标 ----------------
  setEngine(st) {
    const S = window.XQ.state;
    S.engine = st || S.engine;
    const e = S.engine;
    const on = !!e.reachable;
    const b = el('stEngine');
    b.className = 'badge ' + (on ? 'on' : 'off');
    b.textContent = on ? '引擎在线' : '引擎离线';
    const g = el('stGpu');
    if (on && e.gpu) {
      g.style.display = '';
      // 显卡型号：去括号注记、cuda:N 前缀、": 后端名" 后缀与厂商前缀
      // （如 "cuda:0 NVIDIA GeForce RTX 3060 : cudaMallocAsync" → "RTX 3060"）
      g.textContent = String(e.gpu).replace(/\(.*?\)/g, '')
        .replace(/^\s*cuda:\d+\s*/i, '')
        .split(/\s+:\s*/)[0]
        .replace(/^NVIDIA\s+GeForce\s+/i, '')
        .replace(/^(NVIDIA|AMD|Radeon|Intel)\s+/i, '')
        .trim().slice(0, 24) || 'GPU';
    } else {
      g.style.display = 'none';
    }
    const v = el('stVram');
    if (on && e.vram_total) {
      v.style.display = '';
      v.textContent = `显存 ${gb(e.vram_free)} / ${gb(e.vram_total)}`;
    } else {
      v.style.display = 'none';
    }
  },

  /** header 聚合进度条：frac 为 null 隐藏 */

  stateText: (s) => STATE_TXT[s] || s,
};

function gb(n) { return (n / 1073741824).toFixed(1) + 'G'; }

/** 轮询系统状态（引擎 / 显卡徽标） */
export async function pollStatus() {
  const st = await API.soft(API.get('/api/system/status'), '读取状态');
  if (!st) { UI.setEngine({ reachable: false }); return; }
  UI.setEngine(st.engine);
}

// ---------------- 系统状态弹窗（modalStat） ----------------
export async function showStatus() {
  const st = await API.soft(API.get('/api/system/status'), '读取状态');
  if (!st) return;
  const e = st.engine || {};
  const rows = [
    ['应用版本', `v${st.app?.version ?? '–'} · 运行 ${Math.round((st.app?.uptime_s || 0) / 60)} 分钟`],
    ['数据目录', st.app?.data_dir ?? '–'],
    ['引擎地址', esc(e.url || '–')],
    ['引擎连接', e.reachable
      ? `<span class="ok">在线</span>（WS ${e.connected ? '已连接' : '未连接'}）`
      : `<span class="no">离线</span>${e.error ? ' · ' + esc(e.error) : ''}`],
    ['引擎版本', e.version || '–'],
    ['GPU', esc(e.gpu || '–')],
    ['显存', e.vram_total
      ? `${gb(e.vram_free)} 空闲 / ${gb(e.vram_total)} 总量` : '–'],
    ['引擎队列', e.queue
      ? `运行 ${fmtN(e.queue.queue_running)} · 待 ${fmtN(e.queue.queue_pending)}` : '–'],
    ...Object.entries(st.storage || {}).map(([b, h]) => {
      if (b === 'cache')
        return ['存储 · 内部缓存', `${h?.ok ? '<span class="ok">正常</span>' : '<span class="no">异常</span>'} · ${esc(h?.path || '')}`];
      const label = { materials: '素材库（聚合）', materials_images: '图片素材库',
        materials_videos: '视频素材库', materials_audios: '音频素材库',
        results: '成品库' }[b] || b;
      return [`存储 · ${label}`, h?.ok
        ? `<span class="ok">本地</span> · ${esc(h.path || '')}`
        : `<span class="no">异常</span> · ${esc(h?.error || h?.path || '')}`];
    }),
  ];
  UI.showModal('modalStat', `<h3>${icon('chart')}系统状态</h3>
    <div class="env">${rows.map(([k, v]) =>
      `<div class="kv"><b>${k}</b><span>${v}</span></div>`).join('')}</div>`);
}

function fmtN(n) { return +n || 0; }

// 系统状态弹窗挂到 UI（设置弹窗里的「系统状态详情」按钮调用）
UI.showStatus = showStatus;

// 引擎徽标点击 → 状态详情
document.addEventListener('DOMContentLoaded', () => {
  const b = el('stEngine');
  if (!b) return;
  b.style.cursor = 'pointer';
  b.addEventListener('click', () => showStatus().catch(() => {}));
});
