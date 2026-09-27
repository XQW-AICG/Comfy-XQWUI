// segments.js —— 分镜编排：段标签页 / 段编辑面板 / 参考素材区（window.Segments）
//
// 段数据（与后端 workflow/timeline.py 契约一致）：
//   { startFrame, endFrame, prompt, firstFrame:[], lastFrame:[] }
//   参考素材（图片 / 视频 / 音频）为「项目分镜全局通用」池，存于
//   S.refs = { images:[], refVideos:[], refAudios:[] }，全部分镜共享；
//   提交时由 Workbench.collect() 写入每个分镜（后端契约不变）。
// 规则：
//   · 起止帧基于全局帧时间线；段时长 = endFrame_i - endFrame_{i-1}（新增帧模型）
//   · 参考图片不限数量；选择弹窗按文件名关键词拆分 角色 / 环境 / 道具（+其他）宫格
//   · 首帧 / 尾帧合并为一个宫格（每镜独立，各 ≤1）
//   · 帧值显示按 5+17n 网格吸附

import { el, esc, icon, toast, fmtFrames, fmtSec } from './core.js';
import { snapFrames } from './timeline.js';

export const Segments = {
  /** 自动首尾帧续写是否启用：段数 > 1 时自动开启（用户可手动覆盖为关）；
      仅一段时无从续写，恒为关闭。extra 用于预判「新增 N 段后」的段数 */
  contOn(extra = 0) {
    const S = window.XQ.state;
    const multi = (S.segs.length + extra) > 1;
    if (!multi) return false;
    return S.autoCont == null ? true : !!S.autoCont;
  },

  /** 手动开关自动首尾帧续写：同步重整相邻段的引导重叠
      （开启 → 每段承接上一段尾帧 22帧；关闭 → 段首尾相接、各段独立生成） */
  setCont(on) {
    const S = window.XQ.state;
    S.autoCont = !!on;
    for (let i = 1; i < S.segs.length; i++) {
      const prev = S.segs[i - 1], s = S.segs[i];
      s.startFrame = on
        ? Math.max(prev.startFrame + 5, prev.endFrame - 22)
        : prev.endFrame;
      if (s.endFrame - s.startFrame < 5)
        s.endFrame = snapFrames(s.startFrame + 107);
    }
    Segments.renderAll();
    window.Workbench && window.Workbench.autosaveSoon();
  },

  /** 追加一段：默认与上一镜留 22帧 段间引导（续写关闭时首尾相接） */
  add() {
    const S = window.XQ.state;
    const last = S.segs[S.segs.length - 1];
    let start, end;
    if (!last) {
      start = 0; end = 124;
    } else {
      start = Segments.contOn(1) ? Math.max(0, last.endFrame - 22)
        : last.endFrame;
      end = snapFrames(start + 107);
    }
    S.segs.push({ startFrame: start, endFrame: end, prompt: '',
      firstFrame: [], lastFrame: [] });
    S.activeSeg = S.segs.length - 1;
    Segments.renderAll();
    window.Workbench.autosaveSoon();
    const ta = el('segPane') && el('segPane').querySelector('textarea');
    ta && ta.focus();
  },

  remove(i) {
    const S = window.XQ.state;
    if (!confirm(`删除分镜 ${i + 1}？`)) return;
    S.segs.splice(i, 1);
    S.activeSeg = Math.min(S.activeSeg, Math.max(0, S.segs.length - 1));
    Segments.renderAll();
    window.Workbench.autosaveSoon();
  },

  /** 清空为空白工作台（切换到无草稿的新项目时使用）：
      一段空分镜 + 清空全局提示词，并立即自动存档 */
  blank() {
    const S = window.XQ.state;
    S.segs = [];
    S.activeSeg = 0;
    // 全局参考池一并清空（切换到无草稿的新项目时不残留上一项目素材）
    if (window.Media) S.refs = { images: [], refVideos: [], refAudios: [] };
    const g = document.getElementById('gprompt');
    if (g) {
      g.value = '';
      // 程序赋值不触发 input 事件，必须显式重绘背衬高亮层，
      // 否则界面仍显示上一项目的全局提示词（点击后才消失）
      window.Media && window.Media.refresh && window.Media.refresh(g);
    }
    Segments.add();
    Segments.renderAll();
    window.Workbench && window.Workbench.autosaveSoon();
  },

  clearAll() {
    if (!window.XQ.state.segs.length) return;
    if (!confirm('清空全部分镜？（不影响已提交的任务与素材）')) return;
    window.XQ.state.segs = [];
    window.XQ.state.activeSeg = 0;
    Segments.renderAll();
    window.Workbench.autosaveSoon();
  },

  select(i) {
    window.XQ.state.activeSeg = i;
    Segments.renderAll();
    window.Workbench && window.Workbench.autosaveSoon();  // 激活分镜属全局状态
  },

  /** 示例分镜：3 段带提示词与帧布局 */
  loadDemo() {
    const S = window.XQ.state;
    if (S.segs.length && !confirm('覆盖当前分镜？')) return;
    const mk = (start, end, prompt) =>
      ({ startFrame: start, endFrame: end, prompt,
        firstFrame: [], lastFrame: [] });
    S.autoCont = null;             // 示例为全新编排，续写回到「跟随段数自动」
    S.segs = [
      mk(0, 124, '雨夜霓虹街道，少女撑伞回眸，镜头缓推，赛博朋克风，电影感光影'),
      mk(102, 226, '她转身走进小巷，伞面滴落水珠，霓虹灯牌闪烁，慢动作特写'),
      mk(204, 345, '巷口黑猫跃过水洼，少女微笑跟随，镜头上摇至城市天际线，黎明微光'),
    ];
    S.activeSeg = 0;
    Segments.renderAll();
    window.Workbench.autosaveSoon();
    toast('已载入示例分镜（记得在「参考素材」区选择参考图）');
  },

  // ---------------- 渲染 ----------------
  renderAll() {
    Segments.renderGlobalRefs();
    Segments.renderTabs();
    Segments.renderPane();
    window.Timeline.render();
  },

  /** 段有效性（tab 圆点）：有 prompt 且全局参考池或本镜首尾帧非空 */
  _ok(i) {
    const S = window.XQ.state;
    const s = S.segs[i];
    if (!str(s.prompt).trim()) return false;
    const refs = window.Media ? window.Media.ensureRefs() : { images: [] };
    return refs.images.length > 0 || (s.firstFrame || []).length > 0;
  },

  renderTabs() {
    const S = window.XQ.state;
    const fps = S.fps();
    el('segTabs').innerHTML = S.segs.map((s, i) => {
      const st = Segments._ok(i);
      const durS = (s.endFrame - s.startFrame) / fps;
      return `<div class="tab${i === S.activeSeg ? ' on' : ''}"
        onclick="Segments.select(${i})">
        <span class="dot${st ? ' ok' : ''}"></span>分镜 ${i + 1}
        <span class="fr">${fmtFrames(s.startFrame, s.endFrame)} · ${fmtSec(durS)}</span>
      </div>`;
    }).join('');
  },

  renderPane() {
    const S = window.XQ.state;
    const pane = el('segPane');
    const i = S.activeSeg;
    const s = S.segs[i];
    if (!s) {
      pane.innerHTML = `<div class="refempty" style="padding:18px 0">
        没有分镜 —— 点击「加分镜」或「示例」开始编排</div>`;
      return;
    }
    const dur = s.endFrame - s.startFrame;
    const fps = S.fps();
    // 分镜面板内只剩「首尾帧」合并宫格；参考图片 / 视频 / 音频已上移为
    // 项目全局通用（renderGlobalRefs 渲染在工作台顶部，全部分镜共享）
    const body = Segments._framesPanel(i);
    pane.innerHTML = `
      <div class="secline">
        <span class="no" style="font-weight:650;color:var(--acc-h)">分镜 ${i + 1}</span>
        <span class="frlbl">起始</span>
        <input id="segStart" value="${fmtFrames(s.startFrame, s.endFrame)}"
          readonly title="起止帧在下方时间轴拖拽分镜边缘调整">
        <input id="segSec" class="secs" value="${Math.round(dur / fps)}"
          title="本分镜总秒数，可修改（回车或失焦生效，自动吸附帧网格）">秒
        <span class="spacer" style="flex:1"></span>
        <button class="mini danger" onclick="Segments.remove(${i})">${icon('trash', 12)}删除本分镜</button>
      </div>
      <label>画面提示词（本分镜动作 / 镜头描述；全局提示词会自动合并到开头）</label>
      <textarea id="segPrompt" placeholder="例：她转身走进小巷，霓虹灯牌闪烁，慢动作…">${esc(s.prompt)}</textarea>
      <div class="prbar">
        <button class="mini" title="在光标处插入图片 / 音频 / 视频引用"
          onclick="Media.pickRef(t => Media.insertPromptToken('segPrompt', t))">
          ${icon('photo', 12)}插入媒体引用</button>
        <span class="note">直接输入 @ 可引用「参考素材」或本分镜首尾帧（↑↓ 选择、Enter 确认、Esc 取消）；引用以 @素材:文件名 记号嵌入提示词，提交时自动解析为参考素材</span>
      </div>
      <div class="refzone">
        <div class="rzhd"><b>${icon('frames', 13)}首尾帧</b>
          <span class="note">本镜起始 / 结束画面（各 1 个，参考图片 / 视频 / 音频在上方「参考素材」区设置，全部分镜通用）</span>
        </div>
        <div class="rzbody">${body}</div>
      </div>`;
    Segments._bindPane(i);
  },

  // ---------------- 首尾帧合并宫格 ----------------

  /** 首尾帧合并宫格：一个宫格内展示 首帧 / 尾帧 两张卡片（带角色徽标），
      单个「添加」按钮打开合并选择器（弹窗内切换赋值目标） */
  _framesPanel(i) {
    const seg = window.XQ.state.segs[i];
    const slot = (field, label, ic) => {
      const k = (seg[field] || [])[0];
      const a = k && window.Media.assetOf(k);
      return a
        ? `<div class="refitem" title="${esc(a.key)}" ${window.Media.dragAttrs(k)}>
            ${window.Media.thumb(a)}
            <span class="fbadge">${label}</span>
            <button class="rm" title="移除${label}"
              onclick="Segments._unrefF(${i},'${field}')">×</button>
          </div>`
        : `<div class="refitem empty" title="未设置${label}">
            <span class="thumb" style="display:flex;align-items:center;justify-content:center">${icon(ic, 15)}</span>
            <span class="fbadge">${label}</span>
          </div>`;
    };
    return `<div class="refgrp">
      <div class="refitems">${slot('firstFrame', '首帧', 'frames')}${slot('lastFrame', '尾帧', 'film')}
        <button class="refadd" title="选择首帧 / 尾帧素材"
          onclick="Media.picker('frames',${i},event)">${icon('plus', 12)}添加</button>
      </div>
    </div>`;
  },

  // ---------------- 全局参考素材区（项目分镜通用） ----------------

  /** 全局参考素材（图片 / 视频 / 音频）：渲染到工作台顶部 #globalRefs 容器，
      全部分镜共享同一套；提交时由 collect() 写入每个分镜 */
  renderGlobalRefs() {
    const box = el('globalRefs');
    if (!box || !window.Media) return;
    const refs = window.Media.ensureRefs();
    const tab = Segments._refTab;
    const tabBtn = (t, label, n, cap) =>
      `<button type="button" class="rztab${tab === t ? ' on' : ''}"
        onclick="Segments.setRefTab('${t}')">${label} ${n}${cap ? '/' + cap : ''}</button>`;
    const panel = tab === 'images'
      ? Segments._filesPanel('images', '参考图片', 'photo',
        '按文件名关键词分为 角色 / 环境 / 道具 · 不限数量')
      : tab === 'videos'
        ? Segments._filesPanel('refVideos', '参考视频', 'video', '动作 / 运镜迁移 · ≤9')
        : Segments._filesPanel('refAudios', '参考音频', 'music', '音色 / 环境声 · ≤9');
    box.innerHTML = `<div class="rzhd"><b>${icon('photo', 13)}参考素材</b>
        <span class="note">全部分镜通用（提交时应用到每个分镜）</span>
        <span class="spacer" style="flex:1"></span>
        <div class="rztabs">
          ${tabBtn('images', '图片', refs.images.length)}
          ${tabBtn('videos', '视频', refs.refVideos.length, 9)}
          ${tabBtn('audios', '音频', refs.refAudios.length, 9)}
        </div>
      </div>
      <div class="rzbody">${panel}</div>`;
  },

  setRefTab(t) {
    if (!['images', 'videos', 'audios'].includes(t)) return;
    Segments._refTab = t;
    Segments.renderGlobalRefs();
  },

  /** 单组宫格 + 添加按钮（读取全局池）：图片缩略图，视频 / 音频文件条 */
  _filesPanel(field, label, icon_, note) {
    const refs = window.Media.ensureRefs();
    const keys = refs[field] || [];
    const limit = field === 'images' ? null : 9;
    const items = keys.map((k, idx) => Segments._refItem(k, field, idx)).join('');
    return `<div class="refgrp">
      <div class="gt">${icon(icon_, 13)}${label}
        <span class="gtnote">${note}</span>
        <span class="gtcnt">${keys.length}${limit ? '/' + limit : ''}</span></div>
      <div class="refitems">${items}
        <button class="refadd" title="添加素材"
          onclick="Media.picker('${field}',0,event)">${icon('plus', 12)}添加</button>
      </div>
    </div>`;
  },

  /** 全局池单个素材卡片：图片缩略预览，视频 / 音频为文件条，右上角 × 删除 */
  _refItem(k, field, idx) {
    const a = window.Media.assetOf(k);
    const nm = a ? a.key : k;
    const isImg = field === 'images';
    return `<div class="refitem${isImg ? '' : ' file'}" title="${esc(nm)}"
      ${window.Media.dragAttrs(k)}>
      ${a ? window.Media.thumb(a)
          : `<span class="thumb" style="display:flex;align-items:center;justify-content:center">${icon('x', 13)}</span>`}
      ${isImg ? '' : `<span class="nm">${esc(nm)}</span>`}
      <button class="rm" title="移除" onclick="Segments._unrefG('${field}',${idx})">×</button>
    </div>`;
  },

  /** 从全局池移除引用 */
  _unrefG(field, arrIdx) {
    const refs = window.Media.ensureRefs();
    (refs[field] || []).splice(arrIdx, 1);
    Segments.renderAll();
    window.Workbench.autosaveSoon();
  },

  /** 移除本分镜首尾帧（合并宫格用） */
  _unrefF(segIdx, field) {
    const seg = window.XQ.state.segs[segIdx];
    if (!seg) return;
    seg[field] = [];
    Segments.renderAll();
    window.Workbench.autosaveSoon();
  },

  /** 面板输入 → 状态（起止帧只读，由时间轴拖拽调整；prompt / 总秒数实时同步） */
  _bindPane(i) {
    const p = el('segPrompt');
    if (!p) return;
    window.Media.bindRefHighlight(p);         // 本段提示词 @素材 引用高亮
    window.Media.bindCaretPos(p);             // 本段提示词 光标位置指示
    const s = window.XQ.state.segs[i];
    window.Media.enableDrop('segPrompt');     // 支持素材卡拖入插入引用
    p.addEventListener('input', () => {
      s.prompt = p.value;
      Segments.renderTabs();
      window.Timeline.render();
      window.Workbench.autosaveSoon();
    });
    // 总秒数修改 → 重算结束帧（吸附帧网格，受相邻段约束）
    const sec = el('segSec');
    if (sec) {
      const apply = () => {
        const v = parseFloat(sec.value);
        const fps = window.XQ.state.fps();
        if (Number.isFinite(v) && v > 0) {
          const next = window.XQ.state.segs[i + 1];
          let end = snapFrames(s.startFrame + Math.round(v * fps));
          end = Math.max(s.startFrame + 5, end);
          if (next) end = Math.min(end, next.endFrame - 22);
          if (end !== s.endFrame) {
            s.endFrame = end;
            window.Timeline.render();
            Segments.renderTabs();
            window.Workbench.autosaveSoon();
          }
        }
        sec.value = Math.round((s.endFrame - s.startFrame) / window.XQ.state.fps());
      };
      sec.addEventListener('change', apply);
      sec.addEventListener('keydown', e => { if (e.key === 'Enter') sec.blur(); });
    }
  },

  /** 时间轴拖拽后刷新帧 / 秒显示（不重建 DOM，避免打断输入） */
  refreshMeta(i) {
    const s = window.XQ.state.segs[i];
    if (!s) return;
    const fps = window.XQ.state.fps();
    const secOf = f => Math.round(f / fps);
    const a = el('segStart'), sec = el('segSec');
    if (a) a.value = fmtFrames(s.startFrame, s.endFrame);
    if (sec) sec.value = Math.round((s.endFrame - s.startFrame) / fps);
    Segments.renderTabs();
  },
};

function str(x) { return String(x ?? ''); }

/** 按图片方式展示的参考字段（缩略图式；refVideos / refAudios 走文件条式） */
/** 首尾帧字段（合并宫格展示，带角色徽标） */
const FRAME_FIELDS = ['firstFrame', 'lastFrame'];
