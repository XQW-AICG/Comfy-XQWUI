// tasks.js —— 任务中心：列表渲染 / WS 实时更新 / 暂停恢复取消重试删除 / 归档 / 批次报告（window.Tasks）

import { el, esc, icon, toast, fmtTime, fmtSec, VIDEO_EXT, extOf } from './core.js';
import { API } from './api.js';

let cache = [];                 // 任务摘要缓存（含详情标记）
let expanded = new Set();       // 已展开详情的任务 id
let archived = false;           // 当前列表是否为已归档视图

// 错误类别徽标（error_kind → 文案，与后端 task._KINDS 同源）
const KIND_TXT = {
  engine_4xx: '引擎拒绝：配置/模型/素材问题，重试无效，请先调整',
  engine_5xx: '引擎内部错误：可原样重试',
  engine_unreachable: '引擎离线或地址不可达',
  network: '网络连接失败',
  config: '本地配置不合法',
  storage: '存储读写失败',
  disk: '磁盘余量不足',
  queue: '队列深度超限',
  internal: '调度器内部错误',
};
const kindBadge = k => k && KIND_TXT[k]
  ? `<span class="kbadge" title="${KIND_TXT[k]}">${KIND_TXT[k].split('：')[0]}</span> `
  : '';

export const Tasks = {
  open() { window.UI.openTasks(); },

  async reload() {
    const d = await API.soft(API.get('/api/tasks' + (archived ? '?archived=1' : '')), '任务列表');
    if (!d) return;
    cache = d.tasks || [];
    Tasks._render();
    Tasks._headerAggregate();
  },

  // ---------------- WS 实时更新 ----------------
  upsert(summary) {
    const i = cache.findIndex(t => t.id === summary.id);
    if (i >= 0) cache[i] = summary; else cache.unshift(summary);
    Tasks._render();
    Tasks._headerAggregate();
  },

  progress(p) {
    // {id, segment, value, max, stage, detail, eta_s}：段内进度 → 聚合到任务
    const t = cache.find(x => x.id === p.id);
    if (!t) return;
    const segs = t.segments || [];
    if (segs[p.segment]) segs[p.segment]._prog = p;
    const n = segs.length || 1;
    const done = segs.filter(s => s.state === 'done').length;
    let frac = done / n;
    const cur = segs[p.segment];
    if (cur && cur.state !== 'done' && p.max) {
      frac += (p.value / p.max) / n * 0.9;
    }
    t.progress = Math.min(0.99, frac);
    t.stage = p.stage || t.stage;
    t.detail = p.detail || t.detail;
    t.eta_s = p.eta_s ?? t.eta_s;
    t._curSeg = p.segment;
    Tasks._renderProgress(t);
    Tasks._headerAggregate();
  },

  merging(summary) {
    Tasks.upsert({ ...summary, state: summary.state, _merging: true });
    const t = cache.find(x => x.id === summary.id);
    if (t) {
      t._merging = true;
      Tasks._render();
    }
  },

  // ---------------- 操作 ----------------
  /** 工作台「停止任务」入口：当前可停止（排队 / 生成中 / 暂停中）的任务 */
  activeSummaries() {
    return cache.filter(t =>
      ['queued', 'running', 'pausing'].includes(t.state));
  },

  async _act(id, action, label, body) {
    try {
      const r = await API.post(`/api/tasks/${encodeURIComponent(id)}/${action}`, body || {});
      if (r.ok) toast(`${label}成功`, 'ok');
      else toast(`${label}失败：${r.error || '状态不允许'}`, 'err');
      if (r.task) Tasks.upsert(r.task); else Tasks.reload();
    } catch (e) { toast(`${label}失败：${e.message}`, 'err'); }
  },

  /** 段级续跑：从第 i 段（0 基）起重做，该段及之后产物重生成 */
  async retryFrom(id, i) {
    if (!confirm(`将从分镜 ${i + 1} 开始重新生成（该分镜及之后的已有产物会被重做）？`)) return;
    try {
      const r = await API.post(`/api/tasks/${encodeURIComponent(id)}/retry`, { from_segment: i });
      if (!r.ok) return toast('重跑失败：' + (r.error || '状态不允许'), 'err');
      toast(`已从分镜 ${i + 1} 重新入队`, 'ok');
      Tasks.upsert(r.task);
    } catch (e) { toast('重跑失败：' + e.message, 'err'); }
  },

  /** 已归档视图切换 */
  showArchived(on) {
    archived = !!on;
    Tasks.reload();
  },

  async del(id) {
    if (!confirm('删除该任务？（生成结果保留在成品库）')) return;
    try {
      const r = await API.del('/api/tasks/' + encodeURIComponent(id));
      if (!r.ok) return toast('删除失败：' + (r.error || ''), 'err');
      cache = cache.filter(t => t.id !== id);
      expanded.delete(id);
      Tasks._render();
      Tasks._headerAggregate();
      toast('任务已删除', 'ok');
    } catch (e) { toast('删除失败：' + e.message, 'err'); }
  },

  /** 展开/收起详情：拉取完整任务 */
  async toggleDetail(id) {
    if (expanded.has(id)) {
      expanded.delete(id);
      Tasks._render();
      return;
    }
    expanded.add(id);
    const d = await API.soft(API.get('/api/tasks/' + encodeURIComponent(id)), '任务详情');
    if (d) {
      const i = cache.findIndex(t => t.id === id);
      if (i >= 0) cache[i] = { ...cache[i], _detail: d.task };
      Tasks._render();
    }
  },

  // ---------------- 渲染 ----------------
  _render() {
    if (window.XQ.state.view !== 'tasks') { /* 后台更新仍写 DOM，视图隐藏无妨 */ }
    el('taskList').innerHTML = cache.length ? cache.map(Tasks._card).join('')
      : `<div class="tempty">${archived ? '没有已归档任务 —— 在列表中归档完成任务后来此恢复' : '还没有任务 —— 回工作台用「单个生成 / 批量生成」提交分镜视频任务'}</div>`;
  },

  /** 卡片头部单行信息：字段顺序严格固定，竖线「｜」分隔 ——
      生成状态｜项目名称-分镜编号｜任务ID｜分辨率｜帧率｜视频秒数｜提交时间｜
      任务耗时｜保存的文件名称（蓝色高亮，点击跳转成品库对应文件） */
  _hdData(t, stTxt) {
    const cfg = t.config || {};
    const segs = t.segments || [];
    // 项目名称-分镜编号：项目名缺省回退任务名；单段显示段号，多段显示起止段号
    const proj = String(t.project_name || t.name || '未命名').trim() || '未命名';
    let segNo = '';
    if (segs.length === 1) segNo = `分镜${(segs[0].index ?? 0) + 1}`;
    else if (segs.length > 1)
      segNo = `分镜${(segs[0].index ?? 0) + 1}-${(segs[segs.length - 1].index ?? 0) + 1}`;
    // 视频秒数 = 已知段帧数合计 / 帧率（帧数未回填或帧率缺失时显示 –）
    const frames = segs.reduce((n, s) => n + (+s.frames || 0), 0);
    // 保存的文件名称：合并成片优先，单段任务用段产物；仅完成后存在
    const fileKey = (t.result && t.result.merged) ||
      (segs.length === 1 ? segs[0].file : '') || '';
    const fileName = fileKey ? String(fileKey).split('/').pop() : '';
    return {
      fields: [
        stTxt,
        proj + (segNo ? '-' + segNo : ''),
        '#' + t.id.slice(-6),
        cfg.width && cfg.height ? `${cfg.width}×${cfg.height}` : '–',
        cfg.fps ? `${cfg.fps}fps` : '–',
        frames && cfg.fps ? `${Math.round(frames / cfg.fps)}s` : '–',
        fmtTime(t.created_ms),
        Tasks._durTxt(t) || '–',
        fileName || '–',
      ],
      fileKey,
    };
  },

  _hdPlain(t, stTxt) {
    return Tasks._hdData(t, stTxt).fields.join('｜');
  },

  _hdLine(t, stTxt, stCls) {
    const { fields: f, fileKey } = Tasks._hdData(t, stTxt);
    const html = [
      `<span class="tstate ${stCls}">${f[0]}</span>`,
      esc(f[1]),
      `<span class="meta">${esc(f[2])}</span>`,
      `<span class="meta">${f[3]}</span>`,
      `<span class="meta">${f[4]}</span>`,
      `<span class="meta">${f[5]}</span>`,
      `<span class="meta">${f[6]}</span>`,
      `<span class="meta tdur">${f[7]}</span>`,
      fileKey
        ? `<a class="flink" title="在成品库中查看该文件"
            onclick="Tasks.gotoResult('${esc(fileKey)}')">${esc(f[8])}</a>`
        : `<span class="meta">${f[8]}</span>`,
    ];
    return html.join('<span class="hbar">｜</span>');
  },

  /** 跳转成品库并定位高亮对应文件 */
  gotoResult(key) {
    window.Results.goto(key);
  },

  _card(t) {
    const open = expanded.has(t.id);
    const segs = t.segments || [];
    const stCls = t._merging || (t.state === 'running' && t.progress >= 0.99)
      ? 'running' : t.state;
    const stTxt = t._merging ? window.UI.stateText('merging')
      : window.UI.stateText(t.state);
    const pct = Math.round((t.progress || 0) * 100);
    const acts = [];
    if (archived) {
      acts.push(`<button class="mini primary" onclick="Tasks._act('${t.id}','restore','恢复')">${icon('retry', 12)}恢复归档</button>`);
      acts.push(`<button class="mini danger" onclick="Tasks.del('${t.id}')">${icon('trash', 12)}删除</button>`);
      return acts.join('');
    }
    if (t.state === 'queued' || t.state === 'running')
      acts.push(`<button class="mini" onclick="Tasks._act('${t.id}','pause','暂停')">${icon('pause', 12)}暂停</button>`);
    if (t.state === 'paused')
      acts.push(`<button class="mini primary" onclick="Tasks._act('${t.id}','resume','恢复')">${icon('play', 12)}恢复</button>`);
    if (['queued', 'running', 'paused', 'pausing'].includes(t.state))
      acts.push(`<button class="mini danger" onclick="Tasks._act('${t.id}','cancel','取消')">${icon('stop', 12)}取消</button>`);
    if (['error', 'canceled', 'done'].includes(t.state))
      acts.push(`<button class="mini primary" onclick="Tasks._act('${t.id}','retry','重试')">${icon('retry', 12)}重试</button>`);
    if (!['queued', 'running', 'pausing', 'canceling'].includes(t.state))
      acts.push(`<button class="mini" onclick="Tasks._act('${t.id}','archive','归档')" title="归档后从列表隐藏，可随时恢复">${icon('stack', 12)}归档</button>`);
    acts.push(`<button class="mini danger" onclick="Tasks.del('${t.id}')">${icon('trash', 12)}删除</button>`);

    return `<div class="tcard ${t.state === 'error' ? 'err' : ''} ${t.state === 'done' ? 'done' : ''}" id="tc-${t.id}">
      <div class="hd">
        <div class="hline" title="${esc(Tasks._hdPlain(t, stTxt))}">${Tasks._hdLine(t, stTxt, stCls)}</div>
        ${archived ? `<span class="kbadge">已归档</span>` : ''}
        <span class="spacer"></span>
        <button class="mini" onclick="Tasks.toggleDetail('${t.id}')">${icon('eye', 12)}${open ? '收起' : '详情'}</button>
      </div>
      ${Tasks._segsRow(t, segs)}
      <div class="progline">
        <div class="bar" style="flex:1;margin:0"><i style="width:${pct}%"></i></div>
        <span class="prognote">${pct}%</span>
      </div>
      <div class="stline">${Tasks._statusLine(t)}</div>
      ${t.error ? `<div class="errmsg">${kindBadge(t.error_kind)}${esc(t.error)}</div>` : ''}
      <div class="acts">${acts.join('')}</div>
      ${open ? Tasks._detail(t) : ''}
    </div>`;
  },

  /** 实时状态行：阶段 · 实时描述 · 预计剩余时间 · 等待/处理提示 */
  _statusLine(t) {
    const eta = t.eta_s > 0 ? `预计剩余 ${Tasks._fmtEta(t.eta_s)}` : '';
    if (t.state === 'queued')
      return `<span class="stag">排队等待</span><span class="shint">前面的任务完成后自动开始，无需操作</span>`;
    if (t.state === 'pausing')
      return `<span class="stag">暂停中</span><span class="shint">当前分镜跑完后停止，已完成的分镜不会丢失</span>`;
    if (t.state === 'canceling')
      return `<span class="stag">取消中</span><span class="shint">正在中断引擎任务…</span>`;
    if (t.state === 'paused')
      return `<span class="stag">已暂停</span><span class="shint">点「恢复」从未完成的分镜继续，进度已保存</span>`;
    if (t.state === 'error')
      return `<span class="stag">已停止</span><span class="shint">查看下方原因；修正后点「重试」从失败分镜继续</span>`;
    if (t.state === 'done')
      return `<span class="stag">已完成</span><span class="shint">成片已入成品库，可在成品库播放或下载</span>`;
    // running：按后端阶段显示
    const m = {
      sync: '同步素材', submit: '提交工作流', sampling: '引擎生成',
      collect: '回传入库', merge: '合并成片',
    }[t.stage] || '生成中';
    const hint = {
      sync: '首次同步会拷贝参考素材，大视频素材稍慢',
      submit: '正在构建并提交工作流',
      sampling: '首次运行需先加载模型，请耐心等待',
      collect: '正在从引擎回传视频并写入成品库',
      merge: '全部分镜已生成，正在无损拼接；多分镜长视频需数分钟',
    }[t.stage] || '';
    return `<span class="stag">${esc(m)}</span>` +
      `<span class="sdet">${esc(t.detail || '')}</span>` +
      (eta ? `<span class="seta">${eta}</span>` : '') +
      (hint ? `<span class="shint">${esc(hint)}</span>` : '');
  },

  _fmtEta(s) {
    s = Math.max(0, Math.round(s));
    if (s < 60) return fmtSec(s);
    const m = Math.floor(s / 60), r = s % 60;
    if (m < 60) return r ? `${m}分${r}秒` : `${m}分钟`;
    return `${Math.floor(m / 60)}时${m % 60}分`;
  },

  /** 总耗时：running / pausing / canceling 实时累计（起点 started_ms），
      终态取 finished_ms - started_ms；未开始运行返回空（单行中显示 –） */
  _durTxt(t) {
    if (!t.started_ms) return '';
    const end = t.finished_ms ||
      (['running', 'pausing', 'canceling'].includes(t.state) ? Date.now() : 0);
    if (!end) return '';
    return Tasks._fmtDur(end - t.started_ms);
  },

  _fmtDur(ms) {
    let s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return fmtSec(s);
    const m = Math.floor(s / 60); s %= 60;
    if (m < 60) return s ? `${m}分${s}秒` : `${m}分钟`;
    return `${Math.floor(m / 60)}时${m % 60}分`;
  },

  _segsRow(t, segs) {
    if (!segs.length) return '';
    return `<div class="tsegs">${segs.map((s, i) => {
      const cls = s.state === 'running' && s._prog && s._prog.max
        ? ` · ${Math.round(s._prog.value / s._prog.max * 100)}%` : '';
      return `<span class="tseg ${s.state}" title="${esc(s.prompt).slice(0, 60)}">
        ${i + 1} ${stIcon(s.state)}${cls}</span>`;
    }).join('')}</div>`;
  },

  _detail(t) {
    const d = t._detail;
    const segs = d ? (d.segments || []) : (t.segments || []);
    const merged = d && d.result && d.result.url ? d.result : null;
    // 单段任务的成片即段产物本身（同一文件）：详情里不再重复渲染同一视频
    const soloDup = !!(merged && segs.length === 1);
    const segList = soloDup ? [] : segs;
    // 段级续跑入口：失败/已取消/已完成任务可指定起始段重做
    const canRetryFrom = !archived &&
      ['error', 'canceled', 'done'].includes(t.state) && segs.length > 1;
    return `<div class="tdetail">
      ${merged ? `<div class="merged">${icon('film', 15)}
        <b>成片已就绪</b>
        <button class="mini primary" onclick="window.open('${merged.url}','_blank')">${icon('play', 12)}播放</button>
        <a class="note" href="${merged.url}" download>下载</a></div>` : ''}
      ${segList.map((s, i) => `<div class="dseg">
        ${s.url && VIDEO_EXT.includes(extOf(s.url))
          ? `<video src="${s.url}" preload="metadata" controls muted></video>`
          : `<span style="width:200px;display:flex;align-items:center;justify-content:center;height:80px;background:var(--panel);border-radius:9px">${icon('film', 18)}</span>`}
        <div class="di"><b>分镜 ${i + 1} <span class="st-${s.state}">${window.UI.stateText(s.state)}</span></b>
          ${esc(s.prompt || '').slice(0, 120)}
          ${s.frames ? `<br>生成 ${s.frames}帧` : ''}
          ${s.error ? `<br>${kindBadge(s.error_kind)}<span class="no">${esc(s.error)}</span>` : ''}
          ${canRetryFrom ? `<br><button class="mini" onclick="Tasks.retryFrom('${t.id}',${i})" title="重做分镜 ${i + 1} 及之后全部分镜，之前的分镜保留">${icon('retry', 11)}从此分镜重跑</button>` : ''}</div>
      </div>`).join('')}
      ${!d ? '<div class="note" style="margin-top:8px">加载详情…</div>' : ''}
    </div>`;
  },

  /** 单任务进度条局部刷新（避免 WS 高频下整卡重绘打断交互） */
  _renderProgress(t) {
    const card = document.getElementById('tc-' + t.id);
    if (!card) return Tasks._render();
    const pct = Math.round((t.progress || 0) * 100);
    const bar = card.querySelector('.bar>i');
    const note = card.querySelector('.prognote');
    const line = card.querySelector('.stline');
    const dur = card.querySelector('.tdur');
    if (bar) bar.style.width = pct + '%';
    if (note) note.textContent = pct + '%';
    if (dur) dur.textContent = Tasks._durTxt(t);   // 运行耗时实时累计
    if (line) line.innerHTML = Tasks._statusLine(t);
    if (t._curSeg !== undefined) {
      const chip = card.querySelectorAll('.tseg')[t._curSeg];
      if (chip) chip.className = 'tseg running';
    }
  },

  /** 聚合刷新：右下角任务球 + 工作台「停止任务」按钮显隐 */
  _headerAggregate() {
    // 工作台「停止任务」按钮显隐：有可停止任务（排队/生成中/暂停中）才出现
    window.Workbench && window.Workbench.refreshStopBtn(
      cache.filter(t => ['queued', 'running', 'pausing'].includes(t.state)).length);
    Tasks.updateDock();
  },

  /** 右下角任务悬浮球：颜色填充（conic 扇形）= 活跃任务平均进度，
      悬停向左展开显示进行中任务的简约信息；点击进入任务中心。
      由 _headerAggregate 驱动（reload / WS upsert / progress 均会调用） */
  updateDock() {
    const btn = document.getElementById('tdBtn');
    const panel = el('tdPanel');
    if (!btn || !panel) return;
    const act = cache.filter(t =>
      ['queued', 'running', 'pausing', 'canceling'].includes(t.state));
    const pct = act.length
      ? Math.round(act.reduce((s, t) => s + (t.progress || 0), 0)
                   / act.length * 100)
      : 0;
    btn.classList.toggle('busy', act.length > 0);
    btn.style.setProperty('--p', pct);          // 扇形填充比例
    const pctEl = el('tdPct');
    if (pctEl) pctEl.textContent = act.length ? pct + '%' : '';
    panel.innerHTML = act.length
      ? `<div class="tdp-hd">进行中任务 ${act.length} 个 · 点击查看任务中心</div>` +
        act.map(t => {
          const segs = t.segments || [];
          const done = segs.filter(s => s.state === 'done').length;
          const p = Math.round((t.progress || 0) * 100);
          return `<div class="tdp-row" onclick="Tasks.open()">
            <span class="tstate ${t.state}">${window.UI.stateText(t.state)}</span>
            <span class="tdp-nm">${esc(t.name)}</span>
            <span class="tdp-meta">${segs.length
              ? `分镜 ${Math.min(done + 1, segs.length)}/${segs.length}` : ''}</span>
            <div class="bar" style="width:64px;margin:0"><i style="width:${p}%"></i></div>
            <span class="tdp-pct">${p}%</span>
          </div>`;
        }).join('')
      : '<div class="tdp-empty">暂无进行中的任务</div>';
  },

  // ---------------- 批处理报告 ----------------
  async openBatches() {
    let d;
    try { d = await API.get('/api/batches'); } catch (e) {
      return toast('读取批次失败：' + e.message, 'err');
    }
    const rows = (d.batches || []).map(b => {
      const st = Object.entries(b.states || {})
        .map(([k, v]) => `${window.UI.stateText(k)} ${v}`).join(' · ');
      const doneN = (b.states || {}).done || 0;
      const pct = b.total ? Math.round(doneN / b.total * 100) : 0;
      return `<div class="brow">
        <span class="bname">${esc(b.name)} <span class="meta">#${String(b.batch_id).slice(-8)}</span></span>
        <span class="meta">${fmtTime(b.created_ms)} 提交 · ${b.total} 个任务</span>
        <span class="meta">${esc(st)}</span>
        <div class="bar" style="width:120px;margin:0"><i style="width:${pct}%"></i></div>
        <span class="spacer"></span>
        <button class="mini" onclick="Tasks.batchDetail('${b.batch_id}')">${icon('eye', 12)}明细</button>
      </div>`;
    }).join('');
    window.UI.showModal('modalBatch',
      `<h3>${icon('stack')}批处理报告</h3>
       ${rows || '<div class="refempty">暂无批量记录 —— 在工作台用「批量生成」为所有分镜分别提交任务</div>'}`);
  },

  async batchDetail(bid) {
    let d;
    try { d = await API.get('/api/batches/' + encodeURIComponent(bid)); }
    catch (e) { return toast('读取批次明细失败：' + e.message, 'err'); }
    const rows = (d.tasks || []).map(t => {
      const url = t.result && t.result.url ? t.result.url : '';
      return `<div class="brow">
        <span class="tstate ${t.state}">${window.UI.stateText(t.state)}</span>
        <span class="bname">${esc(t.name)}</span>
        <span class="meta">#${t.id.slice(-6)}</span>
        <span class="spacer"></span>
        ${url ? `<a class="mini" href="${url}" target="_blank">${icon('play', 12)}播放</a>
                 <a class="mini" href="${url}" download>下载</a>` : ''}
        ${t.error ? `<span class="meta no">${kindBadge(t.error_kind)}${esc(t.error).slice(0, 80)}</span>` : ''}
      </div>`;
    }).join('');
    window.UI.showModal('modalBatch',
      `<h3>${icon('stack')}批次明细 #${esc(String(bid).slice(-8))}</h3>
       ${rows || '<div class="refempty">批次为空</div>'}`);
  },
};

function stIcon(st) {
  return { done: '✓', running: '●', error: '×', pending: '○' }[st] || '○';
}
