// media.js —— 素材库：清单加载 / 上传 / 选择弹窗（window.Media）

import { el, esc, icon, extOf, toast, IMAGE_EXT, VIDEO_EXT, AUDIO_EXT } from './core.js';
import { API } from './api.js';

const LIMIT = { images: Infinity, refVideos: 9, refAudios: 9 };
const EXTS = { images: IMAGE_EXT, refVideos: VIDEO_EXT, refAudios: AUDIO_EXT,
  frames: IMAGE_EXT };
const TITLES = { images: '选择参考图片', refVideos: '选择参考视频',
  refAudios: '选择参考音频', frames: '选择首帧 / 尾帧素材' };
const ICONF = { images: 'photo', refVideos: 'video', refAudios: 'music',
  frames: 'frames' };

const REFRESH_MS = 4000;      // 选择弹窗打开时的素材库实时同步间隔

// 参考素材为「项目分镜全局通用」池：前端存于 S.refs（全部分镜共享同一套），
// 提交时由 collect() 写入每个分镜。图片不设上限；视频 / 音频各 ≤9。
// 图片选择弹窗按文件名关键词拆分为 角色 / 环境 / 道具（+其他兜底）独立宫格。
// 首帧 / 尾帧合并为一个宫格（每镜独立，各 ≤1），选择器内以目标切换赋值。

// 图片分类关键词（按文件名匹配，命中即归组；都不中归「其他」）
const REF_CATS = [
  { id: 'role', label: '角色', re: /角色|人物|主角|男主|女主|女孩|男孩|少女|少年|骑士|骑士|形象|character|char|person|girl|boy/i },
  { id: 'env', label: '环境', re: /环境|场景|背景|街道|城市|房间|室内|室外|风景|街景|scene|env|background|bg|street|city|room|landscape/i },
  { id: 'prop', label: '道具', re: /道具|物品|工具|武器|prop|item|object|tool|weapon/i },
];

export const Media = {
  /** 拉取素材清单（materials 桶，全量） */
  async load() {
    const d = await API.soft(API.get('/api/assets'), '素材清单');
    if (d) window.XQ.state.assets = d.items || [];
    return d;
  },

  /** 全局参考池（项目分镜通用）：惰性初始化 + 旧数据迁移（v） */
  ensureRefs() {
    const S = window.XQ.state;
    if (!S.refs) S.refs = { images: [], refVideos: [], refAudios: [] };
    return S.refs;
  },

  byKind(kind) {
    const exts = EXTS[kind] || [];
    return window.XQ.state.assets.filter(a => exts.includes(extOf(a.key)));
  },

  assetOf(key) {
    return window.XQ.state.assets.find(a => a.key === key) || null;
  },

  /** 图片分类：返回组 id（role / env / prop / other） */
  classifyImg(key) {
    for (const c of REF_CATS) if (c.re.test(key)) return c.id;
    return 'other';
  },

  /** 选择宫格 HTML：flat=true 单一平铺；否则按 角色/环境/道具/其他 分组 */
  _pickGrid(items, cur, emptyTip, flat = true) {
    if (!items.length)
      return `<div class="refempty" style="grid-column:1/-1">${emptyTip}</div>`;
    if (flat)
      return `<div class="mpgrid-g">${items.map(a => Media._pickItem(a, cur)).join('')}</div>`;
    const groups = [...REF_CATS, { id: 'other', label: '其他' }].map(c => ({
      ...c, items: items.filter(a => Media.classifyImg(a.key) === c.id),
    })).filter(g => g.items.length);
    return groups.map(g =>
      `<div class="mpsec">${esc(g.label)} <span class="note">${g.items.length}</span></div>` +
      `<div class="mpgrid-g">${g.items.map(a => Media._pickItem(a, cur)).join('')}</div>`
    ).join('');
  },

  /** 上传素材（input file，可多选） */
  async upload(input) {
    const files = [...(input.files || [])];
    if (!files.length) return;
    let ok = 0;
    for (const f of files) {
      const r = await API.soft(API.upload('/api/assets', f), '上传 ' + f.name);
      if (r) ok++;
    }
    input.value = '';
    if (ok) {
      toast(`已上传 ${ok} 个素材`, 'ok');
      await Media.load();
      window.Segments.renderAll();
      Media.refreshNow();          // 选择弹窗若打开中，立即同步新素材
    }
  },

  async remove(key) {
    if (!confirm('删除素材「' + key + '」？引用它的位置将同步移除。')) return;
    const r = await API.soft(API.del('/api/assets/' + encodeURIComponent(key)), '删除素材');
    if (r) {
      toast('素材已删除', 'ok');
      const S = window.XQ.state;
      const refs = Media.ensureRefs();
      refs.images = refs.images.filter(k => k !== key);
      refs.refVideos = refs.refVideos.filter(k => k !== key);
      refs.refAudios = refs.refAudios.filter(k => k !== key);
      S.segs.forEach(s => {
        s.firstFrame = (s.firstFrame || []).filter(k => k !== key);
        s.lastFrame = (s.lastFrame || []).filter(k => k !== key);
      });
      await Media.load();
      window.Segments.renderAll();
    }
  },

  // ---------------- 选择弹窗 ----------------
  // 弹窗打开即用缓存秒开，随后自动加载素材库最新清单；打开期间定时同步
  // 文件变化（新增 / 删除 / 路径设置变更），已选状态保持不变。

  _pickTimer: null,
  _pickTick: null,

  /** 弹窗网格实时同步：paint() 以最新清单重绘（实现方负责保留已选状态） */
  _liveRefresh(paint) {
    Media._stopLiveRefresh();
    let sig = null;
    const tick = async () => {
      if (!el('mpGrid')) { Media._stopLiveRefresh(); return; }   // 弹窗已关闭
      const d = await API.get('/api/assets').catch(() => null);  // 后台失败静默重试
      if (!d || !el('mpGrid')) return;
      window.XQ.state.assets = d.items || [];
      const s = (d.items || []).map(a => a.key).join('\n');
      if (s === sig) return;                                     // 无变化不重绘
      sig = s;
      paint();
    };
    Media._pickTick = tick;
    Media._pickTimer = setInterval(tick, REFRESH_MS);
    tick();                                                      // 打开即拉取最新
  },

  _stopLiveRefresh() {
    if (Media._pickTimer) { clearInterval(Media._pickTimer); Media._pickTimer = null; }
    Media._pickTick = null;
  },

  /** 素材清单变化后立即同步打开中的选择弹窗（供上传等流程触发） */
  refreshNow() { Media._pickTick && Media._pickTick(); },

  /** 打开素材选择器。
      · field ∈ images|refVideos|refAudios：读写全局参考池 S.refs（项目分镜
        通用，与分镜号无关）；图片按 角色/环境/道具/其他 分组展示且不限数量，
        视频 / 音频平铺且各 ≤9。
      · field = 'frames'：首尾帧合并选择器（作用于段 segIdx）——弹窗顶部
        切换赋值目标（首帧 / 尾帧），点素材即赋给当前目标（单选替换）。 */
  picker(field, segIdx, ev) {
    if (ev) ev.stopPropagation();
    if (field === 'frames') return Media._framePicker(segIdx);
    const refs = Media.ensureRefs();
    if (!EXTS[field]) return;
    const cur = new Set(refs[field] || []);
    const flat = field !== 'images';
    const emptyTip = '暂无可用素材，请先上传';
    const paint = () => {
      el('mpGrid').innerHTML = Media._pickGrid(Media.byKind(field), cur,
        emptyTip, flat);
    };
    const capNote = LIMIT[field] === Infinity
      ? '不限数量' : `最多 ${LIMIT[field]} 个`;
    const html = `
      <h3>${icon(ICONF[field])}${TITLES[field]}
        <span class="note">（全部分镜通用 · ${capNote}）</span>
        <span class="spacer"></span></h3>
      <div class="mpbar">
        <label class="mpasset">${icon('upload', 13)}上传
          <input type="file" multiple accept="${Media._accept(field)}"
                 style="display:none" onchange="Media.upload(this)">
        </label>
        <span class="note">${field === 'images'
          ? '素材保存到工作台存储；按文件名关键词自动归入 角色 / 环境 / 道具 分组'
          : '展示已设置的素材库路径内容；上传的素材自动归入对应分类'}</span>
      </div>
      <div class="mpgrid" id="mpGrid">${Media._pickGrid(Media.byKind(field),
        cur, emptyTip, flat)}</div>
      <div class="mfoot">
        <span class="note" style="margin-right:auto" id="mpCount"></span>
        <button onclick="UI.closeModal('modalPick')">取消</button>
        <button class="primary" id="mpOk">${icon('check')}确定</button>
      </div>`;
    UI.showModal('modalPick', html);

    const grid = el('mpGrid');
    grid.addEventListener('click', e => {
      const it = e.target.closest('.mpitem');
      if (!it) return;
      const key = it.dataset.key;
      if (cur.has(key)) cur.delete(key);
      else if (cur.size >= LIMIT[field]) {
        toast(`最多选择 ${LIMIT[field]} 个`, 'err'); return;
      } else cur.add(key);
      it.classList.toggle('sel', cur.has(key));
      el('mpCount').textContent = `已选 ${cur.size}`;
    });
    el('mpCount').textContent = `已选 ${cur.size}`;
    el('mpOk').onclick = () => {
      refs[field] = [...cur];
      Media._stopLiveRefresh();
      UI.closeModal('modalPick');
      window.Segments.renderAll();
      window.Workbench.autosaveSoon();
    };
    Media._liveRefresh(paint);
  },

  /** 首尾帧合并选择器：一个宫格，顶部切换赋值目标（首帧 / 尾帧），
      点素材即赋给当前目标（单选替换）；确定时写回该分镜的两个帧字段 */
  _framePicker(segIdx) {
    const S = window.XQ.state;
    const seg = S.segs[segIdx];
    if (!seg) return;
    seg.firstFrame = seg.firstFrame || [];
    seg.lastFrame = seg.lastFrame || [];
    let target = seg.firstFrame.length ? 'lastFrame' : 'firstFrame';
    const paint = () => {
      const cur = new Set([...seg.firstFrame, ...seg.lastFrame]);
      el('mpGrid').innerHTML = Media._pickGrid(Media.byKind('frames'), cur,
        '暂无可用素材，请先上传', true);
      el('mpCount').textContent =
        `首帧：${seg.firstFrame[0] || '未设置'} · 尾帧：${seg.lastFrame[0] || '未设置'}`;
    };
    const html = `
      <h3>${icon('frames')}选择首帧 / 尾帧素材
        <span class="note">（分镜 ${segIdx + 1} · 各 1 个）</span>
        <span class="spacer"></span></h3>
      <div class="mpbar">
        <span class="note" style="margin-right:6px">赋值目标：</span>
        <label class="ck"><input type="radio" name="frameTgt" value="firstFrame"
          ${target === 'firstFrame' ? 'checked' : ''}> 首帧（起始画面）</label>
        <label class="ck"><input type="radio" name="frameTgt" value="lastFrame"
          ${target === 'lastFrame' ? 'checked' : ''}> 尾帧（结束画面）</label>
        <label class="mpasset" style="margin-left:auto">${icon('upload', 13)}上传
          <input type="file" multiple accept="${Media._accept('frames')}"
                 style="display:none" onchange="Media.upload(this)">
        </label>
      </div>
      <div class="mpgrid" id="mpGrid"></div>
      <div class="mfoot">
        <span class="note" style="margin-right:auto" id="mpCount"></span>
        <button onclick="UI.closeModal('modalPick')">取消</button>
        <button class="primary" id="mpOk">${icon('check')}确定</button>
      </div>`;
    UI.showModal('modalPick', html);
    document.querySelectorAll('input[name="frameTgt"]').forEach(r =>
      r.addEventListener('change', () => { target = r.value; }));
    const grid = el('mpGrid');
    grid.addEventListener('click', e => {
      const it = e.target.closest('.mpitem');
      if (!it) return;
      seg[target] = [it.dataset.key];       // 单选替换当前目标
      const other = target === 'firstFrame' ? 'lastFrame' : 'firstFrame';
      if (seg[other][0] === it.dataset.key) seg[other] = [];  // 不允许同图双帧
      paint();
    });
    paint();
    el('mpOk').onclick = () => {
      Media._stopLiveRefresh();
      UI.closeModal('modalPick');
      window.Segments.renderAll();
      window.Workbench.autosaveSoon();
    };
    Media._liveRefresh(paint);
  },

  _accept(field) {
    return EXTS[field].map(e => '.' + e).join(',');
  },

  // ---------------- 素材拖拽引用 ----------------
  /** 素材卡拖拽源属性（draggable + dragstart 写入素材 key） */
  dragAttrs(key) {
    return `draggable="true" ondragstart="Media.dragStart(event,'${esc(key)}')"`;
  },

  dragStart(ev, key) {
    ev.dataTransfer.setData('text/xq-asset', key);
    ev.dataTransfer.setData('text/plain', '@素材:' + key);
    ev.dataTransfer.effectAllowed = 'copy';
  },

  /** 把编辑框注册为素材投放目标：落点在当前光标处插入 @素材: 记号（幂等绑定） */
  enableDrop(taId) {
    const ta = el(taId);
    if (!ta || ta.dataset.dropBound) return;
    ta.dataset.dropBound = '1';
    ta.addEventListener('dragover', e => {
      if (![...e.dataTransfer.types].includes('text/xq-asset')) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
      ta.classList.add('drop-hot');
    });
    ta.addEventListener('dragleave', () => ta.classList.remove('drop-hot'));
    ta.addEventListener('drop', e => {
      ta.classList.remove('drop-hot');
      const key = e.dataTransfer.getData('text/xq-asset');
      if (!key) return;                       // 非素材拖拽走浏览器默认行为
      e.preventDefault();
      if (Media.insertToken(taId, '@素材:' + key) == null) return;
      toast('已插入素材引用：@素材:' + key, 'ok');
    });
  },

  /** 媒体预览块：图片直接预览；视频渲染首帧（浏览器不支持时回落图标）；音频显示图标 */
  _preview(a, size) {
    const ext = extOf(a.key);
    if (IMAGE_EXT.includes(ext))
      return `<img class="thumb" loading="lazy" src="${a.url}">`;
    if (VIDEO_EXT.includes(ext))
      return `<video class="thumb" loading="lazy" preload="metadata" muted
        src="${a.url}#t=0.1"
        onerror="this.outerHTML=window.Media._fallback('${esc(ext)}',${size})"></video>`;
    return Media._fallback(ext, size);
  },

  /** 非预览媒体的图标占位 */
  _fallback(ext, size) {
    return `<span class="thumb" style="display:flex;align-items:center;justify-content:center">${
      icon(AUDIO_EXT.includes(ext) ? 'music' : 'film', size)}</span>`;
  },

  _pickItem(a, cur) {
    return `<div class="mpitem${cur.has(a.key) ? ' sel' : ''}" data-key="${esc(a.key)}"
      ${Media.dragAttrs(a.key)}>
      ${Media._preview(a, 20)}<b>${esc(a.key)}</b></div>`;
  },

  /** 分镜面板里的参考素材缩略块（图片网格 / 视频音频文件条） */
  thumb(a) {
    return Media._preview(a, 15);
  },

  // ---------------- 提示词媒体引用 ----------------
  _pickRefCb: null,

  /** 提示词引用选择器：单选任意素材（图片/音频/视频）或输入本机路径，
      确定后以 @素材: 记号回调（cb(token)）；打开即自动加载，打开期间实时同步 */
  pickRef(cb) {
    Media._pickRefCb = cb || null;
    let sel = null;
    const paint = () => {
      const items = window.XQ.state.assets;
      el('mpGrid').innerHTML = Media._pickGrid(items,
        new Set(sel ? [sel] : []), '素材库为空 —— 可上传，或直接填写本机文件路径');
    };
    const items = window.XQ.state.assets;
    const html = `
      <h3>${icon('photo')}插入媒体引用
        <span class="note">（图片 / 音频 / 视频）</span><span class="spacer"></span></h3>
      <div class="mpbar">
        <label class="mpasset">${icon('upload', 13)}上传新素材
          <input type="file" multiple style="display:none"
            onchange="Media.upload(this)">
        </label>
        <span class="note">选中素材或填写路径，将以 @素材: 记号插入提示词</span>
      </div>
      <div class="mpgrid" id="mpGrid">${Media._pickGrid(items, new Set(), '素材库为空 —— 可上传，或直接填写本机文件路径')}
      </div>
      <label style="margin-top:10px">或指定文件路径（本机路径，任务提交时自动导入素材库）</label>
      <input id="mpPath" placeholder="/home/user/clips/demo.mp4">
      <div class="mfoot">
        <span class="note" style="margin-right:auto">单选；已选素材优先于路径</span>
        <button onclick="UI.closeModal('modalPick')">取消</button>
        <button class="primary" id="mpOk">${icon('check')}插入</button>
      </div>`;
    UI.showModal('modalPick', html);

    const grid = el('mpGrid');
    grid.addEventListener('click', e => {
      const it = e.target.closest('.mpitem');
      if (!it) return;
      grid.querySelectorAll('.mpitem.sel').forEach(n => n.classList.remove('sel'));
      it.classList.add('sel');
      sel = it.dataset.key;
    });
    el('mpOk').onclick = () => {
      const key = sel || el('mpPath').value.trim();
      if (!key) { toast('请选择素材或填写文件路径', 'err'); return; }
      Media._stopLiveRefresh();
      UI.closeModal('modalPick');
      Media._pickRefCb && Media._pickRefCb('@素材:' + key);
    };
    Media._liveRefresh(paint);
  },

  /** 在 textarea 光标处插入记号并派发 input 事件（驱动状态同步与自动存档）。
      记号前后自动补空白分隔 —— 前后空白标识这是一段引用素材，
      提交解析时记号被移除，多余空白由后端自动清理 */
  insertToken(taId, token) {
    const ta = el(taId);
    if (!ta) return null;
    const s = ta.selectionStart ?? ta.value.length;
    const e = ta.selectionEnd ?? s;
    const before = ta.value.slice(0, s);
    const after = ta.value.slice(e);
    const padL = before && !/\s$/.test(before) ? ' ' : '';
    const padR = after && !/^\s/.test(after) ? ' ' : '';
    const full = padL + token + padR;
    ta.value = before + full + after;
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    ta.focus();
    ta.selectionStart = ta.selectionEnd = s + full.length;
    ta._refHlPaint && ta._refHlPaint(true);   // 插入后光标落到记号末尾并立即重绘
    ta._caretPaint && ta._caretPaint();
    return ta.value;
  },

  /** 提示词插入入口（全局 / 分镜通用） */
  insertPromptToken(taId, token) {
    if (Media.insertToken(taId, token) == null) return;
    toast('已插入媒体引用：' + token, 'ok');
  },

  // ---------------- 编辑框光标位置指示 ----------------
  // 右下角悬浮角标实时显示当前编辑位置：光标处「行 x · 列 y」，
  // 有选区时显示「已选 N 字」。须在 bindRefHighlight 之后调用
  //（角标挂在背衬 wrap 上，随输入框一起布局；幂等绑定）
  bindCaretPos(ta) {
    if (!ta || ta.dataset.caretpos) return;
    ta.dataset.caretpos = '1';
    Media.bindClickFocus(ta);            // 单击即进入编辑态并显示光标
    Media.bindMention(ta);               // 输入 @ 直接唤起素材候选
    const badge = document.createElement('span');
    badge.className = 'caretpos';
    badge.textContent = '行 1 · 列 1';
    ta.parentNode.appendChild(badge);
    const paint = () => {
      const a = ta.selectionStart ?? 0, b = ta.selectionEnd ?? 0;
      if (a !== b) {
        badge.textContent = '已选 ' + (b - a) + ' 字';
      } else {
        const lines = ta.value.slice(0, a).split('\n');
        badge.textContent = '行 ' + lines.length +
          ' · 列 ' + (lines[lines.length - 1].length + 1);
      }
      badge.style.opacity = document.activeElement === ta ? '1' : '.55';
      ta._refHlPaint && ta._refHlPaint();   // 同步重绘背衬层的白色闪烁光标
    };
    ta.addEventListener('input', paint);
    ta.addEventListener('click', paint);
    ta.addEventListener('keyup', paint);
    ta.addEventListener('focus', paint);
    ta.addEventListener('blur', paint);
    // 方向键 / 拖拽选择等不触发 keyup 的情况由 selectionchange 兜底
    document.addEventListener('selectionchange', () => {
      if (document.activeElement === ta) paint();
    });
    ta._caretPaint = paint;              // 供单击聚焦等外部逻辑主动刷新
    paint();
  },

  // ---------------- 单击即进入编辑态 ----------------
  /** 单击编辑框任意位置（含背衬层与内边距）立即聚焦并显示光标：
      点击文本区域由浏览器原生定位光标；点击内边距 / 边框等非文本区域时
      手动聚焦并把光标落到点击处，坐标无法解析时落到文末（幂等绑定） */
  bindClickFocus(ta) {
    if (!ta || ta.dataset.clickfocus) return;
    ta.dataset.clickfocus = '1';
    ta.readOnly = false;                 // 确保处于可编辑状态
    ta.removeAttribute('readonly');
    const offsetAt = (x, y) => {
      // 坐标 → 文本偏移（标准 / WebKit 双实现；解析不到返回 null）
      let node = null, off = 0;
      if (document.caretPositionFromPoint) {
        const pos = document.caretPositionFromPoint(x, y);
        if (pos) { node = pos.offsetNode; off = pos.offset; }
      } else if (document.caretRangeFromPoint) {
        const rg = document.caretRangeFromPoint(x, y);
        if (rg) { node = rg.startContainer; off = rg.startOffset; }
      }
      if (!node || node.nodeType !== 3) return null;   // 仅采信文本节点命中
      return Math.max(0, Math.min(off, ta.value.length));
    };
    const focusAt = (x, y) => {
      ta.focus({ preventScroll: true });
      const at = (x == null) ? null : offsetAt(x, y);
      const p = (at == null) ? ta.value.length : at;
      try { ta.selectionStart = ta.selectionEnd = p; } catch { /* 忽略 */ }
      ta._caretPaint && ta._caretPaint();
    };
    // 事件挂在外层 wrap（背衬层容器）上，每次按当前父节点取，避免重排失效
    const onDown = e => {
      if (e.target === ta) return;       // 文本区域交给浏览器原生定位
      e.preventDefault();                // 阻止选区丢失，改由下面定位光标
      focusAt(e.clientX, e.clientY);
    };
    (ta.parentElement || ta).addEventListener('mousedown', onDown);
    // 兜底：任何来源的单击都保证进入可编辑态并显示光标
    ta.addEventListener('click', () => {
      if (document.activeElement !== ta) focusAt(null, null);
    });
    ta.addEventListener('mouseup', () => ta._caretPaint && ta._caretPaint());
  },

  /** 外部改写编辑框 value 后刷新呈现层。
      提示词文字是透明的、由背衬层着色渲染，直接赋值 value 不会触发重绘，
      必须显式重绘背衬层（含 @素材 高亮）与光标角标，否则内容要到用户
      首次点击 / 输入时才显示出来 */
  refresh(ta) {
    if (!ta) return;
    ta._refHlPaint && ta._refHlPaint(true);
    ta._caretPaint && ta._caretPaint();
  },

  // ---------------- 编辑框内 @ 引用素材（输入 @ 即出候选） ----------------
  // 光标前出现「行首或空白后的 @」且其后是连续查询词时，在编辑框下方浮出
  // 素材候选：↑↓ 选择、Enter / Tab 或点击确认、Esc 取消，确认后把已输入的
  // @查询词整体替换为 @素材:文件名 记号（幂等绑定，与插入按钮结果一致）。
  // 候选范围仅为「素材参考」中已选中的素材，不提供素材库直选
  _mentionMax: 8,

  _iconOf(key) {
    const e = extOf(key);
    if (IMAGE_EXT.includes(e)) return 'photo';
    if (VIDEO_EXT.includes(e)) return 'video';
    if (AUDIO_EXT.includes(e)) return 'music';
    return 'stack';
  },

  /** 光标处的 @ 触发态：{start: '@' 下标, query: 查询词}；不在触发态返回 null */
  _mentionAt(ta) {
    const a = ta.selectionStart ?? 0, b = ta.selectionEnd ?? a;
    if (a !== b) return null;                       // 有选区时不触发
    const head = ta.value.slice(0, a);
    const at = head.lastIndexOf('@');
    if (at < 0) return null;
    if (at > 0 && !/[\s，。；、,;：！？（）【】《》「」]/.test(head[at - 1]))
      return null;                                  // @ 必须位于行首或空白 / 句读之后
    const q = head.slice(at + 1);
    if (!/^[^@\s:：]{0,40}$/.test(q)) return null;  // 查询词内不能有空白与冒号
    return { start: at, query: q };
  },

  /** 可引用的素材 = 全局参考池 + 当前分镜首尾帧（不读取素材库）。
      参考素材已是项目分镜全局通用，两个编辑框范围一致；去重保序 */
  _mentionItems(ta, q) {
    const S = window.XQ.state;
    const refs = Media.ensureRefs();
    const seg = (ta && ta.id === 'segPrompt')
      ? S.segs[S.activeSeg] : null;
    const keys = [...refs.images, ...refs.refVideos, ...refs.refAudios,
      ...((seg && seg.firstFrame) || []), ...((seg && seg.lastFrame) || [])];
    const s = (q || '').toLowerCase();
    const rank = k => {
      const lk = String(k).toLowerCase();
      const i = lk.indexOf(s);
      return (i < 0 ? 999 : i) * 1000 + lk.length;
    };
    return [...new Set(keys.filter(Boolean))]
      .filter(k => !s || String(k).toLowerCase().includes(s))
      .sort((x, y) => rank(x) - rank(y) || String(x).localeCompare(String(y)))
      .slice(0, Media._mentionMax)
      .map(key => ({ key }));
  },

  bindMention(ta) {
    if (!ta || ta.dataset.mention) return;
    ta.dataset.mention = '1';
    const panel = document.createElement('div');
    panel.className = 'mpop hide';
    (ta.parentElement || ta).appendChild(panel);
    let items = [], cur = 0;
    const open = () => !panel.classList.contains('hide');
    const close = () => { panel.classList.add('hide'); items = []; cur = 0; };
    const paint = () => {
      const m = Media._mentionAt(ta);
      if (!m) { close(); return; }
      items = Media._mentionItems(ta, m.query);   // 仅素材参考中已选的素材
      if (!items.length) { close(); return; }
      cur = Math.max(0, Math.min(cur, items.length - 1));
      panel.innerHTML = items.map((a, i) =>
        `<div class="mrow${i === cur ? ' on' : ''}" data-i="${i}"
          >${icon(Media._iconOf(a.key), 13)}<span class="nm">${
          esc(a.key)}</span></div>`).join('');
      panel.classList.remove('hide');
    };
    const move = d => {
      if (!items.length) return;
      cur = (cur + d + items.length) % items.length;
      paint();
    };
    const accept = (i) => {
      const m = Media._mentionAt(ta), a = items[i == null ? cur : i];
      if (!m || !a) return false;
      const pos = ta.selectionStart ?? ta.value.length;
      const token = '@素材:' + a.key + ' ';         // 尾随空白：与相邻正文分隔
      ta.value = ta.value.slice(0, m.start) + token + ta.value.slice(pos);
      close();
      ta.selectionStart = ta.selectionEnd = m.start + token.length;
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      ta.focus();
      ta._refHlPaint && ta._refHlPaint(true);
      ta._caretPaint && ta._caretPaint();
      return true;
    };
    ta.addEventListener('input', paint);
    ta.addEventListener('click', paint);
    ta.addEventListener('keyup', e => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) paint();
    });
    ta.addEventListener('keydown', e => {
      if (!open()) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
      else if (e.key === 'Enter' || e.key === 'Tab') {
        if (accept()) e.preventDefault();           // 确认时不插入换行
      } else if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
    // mousedown 早于 blur：阻止默认可避免编辑框失焦导致候选提前关闭
    panel.addEventListener('mousedown', e => {
      const it = e.target.closest('.mrow');
      if (!it) return;
      e.preventDefault();
      accept(+it.dataset.i);
    });
    ta.addEventListener('blur', () => setTimeout(close, 150));
    document.addEventListener('selectionchange', () => {
      if (document.activeElement === ta) paint();
    });
  },

  // ---------------- @素材 引用蓝色高亮 ----------------
  // textarea 原生不支持富文本：文字透明，由下方同步滚动的背衬层负责着色，
  // @素材: 记号以蓝色高亮渲染（幂等绑定，输入 / 滚动自动重绘）
  _refHlRe: /@素材:[^\s@，。；、,;：！？（）【】《》「」『』“”‘’…!?"']*/g,

  _refHlHtml(text) {
    return esc(text).replace(Media._refHlRe,
      m => '<span class="refhl">' + m + '</span>');
  },

  /** 背衬层内容：高亮文本 + 光标处的白色闪烁竖条。
      原生光标在透明文字下不可见且闪烁不受控，改由背衬层内联竖条呈现：
      竖条随文本自然排布（换行 / 滚动自动跟随），失焦或有选区时不画 */
  _hlHtmlWithCaret(ta) {
    const v = ta.value;
    const a = ta.selectionStart ?? 0, b = ta.selectionEnd ?? a;
    if (document.activeElement !== ta || a !== b)
      return Media._refHlHtml(v) + '\n';
    return Media._refHlHtml(v.slice(0, a)) +
      '<span class="caretmark"></span>' +
      Media._refHlHtml(v.slice(a)) + '\n';
  },

  bindRefHighlight(ta) {
    if (!ta || ta.dataset.refhl) return;
    ta.dataset.refhl = '1';
    const cs = getComputedStyle(ta);
    const wrap = document.createElement('div');
    wrap.style.cssText = 'position:relative';
    const hl = document.createElement('div');
    hl.setAttribute('aria-hidden', 'true');
    hl.style.cssText = [
      'position:absolute', 'inset:0', 'pointer-events:none', 'overflow:hidden',
      'white-space:pre-wrap', 'overflow-wrap:break-word',
      'border:1px solid transparent',
      'font-family:' + cs.fontFamily, 'font-size:' + cs.fontSize,
      'font-weight:' + cs.fontWeight, 'font-style:' + cs.fontStyle,
      'letter-spacing:' + cs.letterSpacing,
      'line-height:' + cs.lineHeight, 'padding:' + cs.padding,
      'border-radius:' + cs.borderRadius, 'box-sizing:' + cs.boxSizing,
      'color:' + cs.color,
    ].join(';');
    ta.parentNode.insertBefore(wrap, ta);
    wrap.appendChild(hl);
    wrap.appendChild(ta);
    // 输入框文字透明，由背衬层显示；原生光标置透明，改由背衬层的
    // 白色闪烁竖条（.caretmark）呈现，选区仍由输入框原生高亮
    ta.style.background = 'transparent';
    ta.style.color = 'transparent';
    ta.style.caretColor = 'transparent';
    // 内容未变化时不重绘，避免闪烁动画被反复打断（保持稳定的 1s 呼吸节奏）
    const paint = (force) => {
      const key = [ta.value, ta.selectionStart, ta.selectionEnd,
        document.activeElement === ta ? 1 : 0].join('\u0000');
      if (force !== true && key === ta._refHlKey) {
        hl.scrollTop = ta.scrollTop; return;
      }
      ta._refHlKey = key;
      hl.innerHTML = Media._hlHtmlWithCaret(ta);
      hl.scrollTop = ta.scrollTop;
    };
    ta._refHlPaint = paint;              // 供光标移动 / 聚焦时重绘（含闪烁竖条）
    // 光标竖条自包含：输入 / 点击 / 方向键 / 聚焦 / 失焦 / 滚动 全部重绘，
    // 不依赖其他绑定（角标未绑定时也照样显示白色闪烁光标）
    ['input', 'click', 'mouseup', 'keyup', 'focus', 'blur', 'scroll']
      .forEach(ev => ta.addEventListener(ev, paint));
    document.addEventListener('selectionchange', () => {
      if (document.activeElement === ta) paint();
    });
    paint(true);
  },
};
