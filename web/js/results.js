// results.js —— 成品库：清单 / 播放器 / 多选删除（window.Results）

import { el, esc, icon, toast, fmtBytes, fmtTime, extOf,
         VIDEO_EXT, IMAGE_EXT } from './core.js';
import { API } from './api.js';

let items = [];

export const Results = {
  reload() { Results.load(window.XQ.state.resultsKind); },

  /** kind ∈ videos|images：拉取并渲染 */
  async load(kind) {
    const S = window.XQ.state;
    S.resultsKind = kind || S.resultsKind;
    const d = await API.soft(API.get('/api/results?limit=500'), '成品清单');
    items = (d ? d.items : []).filter(o => {
      const e = extOf(o.key);
      return S.resultsKind === 'videos' ? VIDEO_EXT.includes(e)
        : IMAGE_EXT.includes(e);
    });
    // 去重：段产物与合并成片内容相同、名称不同 —— 已有成片时隐藏段产物
    // （段产物仍在磁盘与任务详情中保留，此处仅控制成品库展示）
    //  · 新命名：{前缀}_{短id}_sNN.mp4 vs {前缀}_{短id}.mp4
    //    → 去掉 _sNN 后的同名文件存在即隐藏
    //  · 旧命名：{任务id}/segNN.mp4 vs {任务id}/merged.mp4
    //    → 任务下已有 merged.mp4 即隐藏
    const keySet = new Set(items.map(o => o.key));
    items = items.filter(o => {
      const m = o.key.match(/^(.*_s\d{2})\.(\w+)$/);
      if (m && keySet.has(m[1] + "." + m[2])) return false;
      if (/(^|\/)seg\d+\./i.test(o.key)) {
        const g = o.key.slice(0, o.key.lastIndexOf("/"));
        if (g && keySet.has(g + "/merged.mp4")) return false;
      }
      return true;
    });
    S.resultsSel = new Set();
    Results._render();
  },

  selectAll(on) {
    const S = window.XQ.state;
    S.resultsSel = on ? new Set(items.map(o => o.key)) : new Set();
    Results._render();
  },

  async deleteSelected() {
    const S = window.XQ.state;
    const keys = [...S.resultsSel];
    if (!keys.length) return toast('未选中任何成品', 'err');
    if (!confirm(`删除选中的 ${keys.length} 个成品？（不可恢复）`)) return;
    let ok = 0;
    for (const k of keys) {
      try {
        const r = await API.del('/api/results/' + encodeURIComponent(k));
        if (r && r.ok) ok++;
      } catch { /* 单个失败继续 */ }
    }
    toast(`已删除 ${ok}/${keys.length} 个成品`, ok ? 'ok' : 'err');
    Results.load(S.resultsKind);
  },

  play(key) {
    const o = items.find(x => x.key === key);
    if (!o) return;
    const isVid = VIDEO_EXT.includes(extOf(key));
    el('player').innerHTML = `
      <div class="phead"><b>${icon(isVid ? 'film' : 'photo', 14)} ${esc(o.key)}</b>
        <span class="spacer"></span>
        <a class="note" href="${o.url}" download>下载</a>
        <button class="mini" onclick="Results.closePlayer()">${icon('x', 12)}关闭</button>
      </div>
      ${isVid ? `<video src="${o.url}" controls autoplay loop></video>`
              : `<img src="${o.url}" alt="">`}
      <div class="meta">${fmtBytes(o.size)} · ${fmtTime(o.created_ms)}</div>`;
    el('player').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  },

  closePlayer() { el('player').innerHTML = ''; },

  /** 从任务卡片跳转：切到成品库（视频页签）刷新后，滚动定位并高亮对应成品 */
  async goto(key) {
    const S = window.XQ.state;
    window.UI.nav('lib');
    await Results.load(S.resultsKind === 'images' ? 'images' : 'videos');
    const card = [...document.querySelectorAll('#lib .card')].find(c => {
      const cb = c.querySelector('.pick input');
      return cb && cb.dataset.k === key;
    });
    if (!card) return toast('成品库中未找到：' + key, 'err');
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.add('flash');
    setTimeout(() => card.classList.remove('flash'), 3600);
  },

  _render() {
    const S = window.XQ.state;
    el('libCount').textContent = `${items.length} 个${S.resultsKind === 'videos' ? '视频' : '图片'}`;
    el('libSelN').textContent = S.resultsSel.size ? ` (${S.resultsSel.size})` : '';
    el('lib').innerHTML = items.length ? items.map(o => {
      const isVid = VIDEO_EXT.includes(extOf(o.key));
      return `<div class="card${S.resultsSel.has(o.key) ? ' sel' : ''}">
        <span class="pick"><input type="checkbox" data-k="${esc(o.key)}"
          ${S.resultsSel.has(o.key) ? 'checked' : ''}
          onchange="Results._pick('${esc(o.key)}',this.checked)" onclick="event.stopPropagation()"></span>
        <div class="th" onclick="Results.play('${esc(o.key)}')">
          ${isVid ? `<video src="${o.url}" preload="metadata" muted></video>`
                  : `<img src="${o.url}" loading="lazy" alt="">`}</div>
        <div class="nm" title="${esc(o.key)}">${esc(o.key)}</div>
      </div>`;
    }).join('')
      : `<div class="tempty" style="grid-column:1/-1">成品库为空 —— 任务完成后视频自动归档到这里</div>`;
  },

  _pick(key, on) {
    const S = window.XQ.state;
    if (on) S.resultsSel.add(key); else S.resultsSel.delete(key);
    el('libSelN').textContent = S.resultsSel.size ? ` (${S.resultsSel.size})` : '';
    document.querySelectorAll('#lib .card').forEach(c => {
      const cb = c.querySelector('.pick input');
      if (cb && cb.dataset.k === key) c.classList.toggle('sel', cb.checked);
    });
  },
};
