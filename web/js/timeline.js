// timeline.js —— 底部时间轴：渲染段落块、拖拽调整起止帧（5+17n 网格吸附）、
// 段间引导帧计算展示（与后端 workflow/timeline.py 同语义）

import { el, esc, icon, toast, fmtFrames, fmtSec } from './core.js';
import { API } from './api.js';

const GRID_BASE = 5, GRID_STEP = 17;

/** 向上吸附到 5+17n 帧网格 */
export function snapFrames(n) {
  n = Math.max(GRID_BASE, Math.round(+n || GRID_BASE));
  while (n % GRID_STEP !== GRID_BASE) n += 1;
  return n;
}

/** 段间引导帧吸附：候选 {5,22,39,...} 就近、并列取大（与后端一致） */
export function snapContinuity(n) {
  n = Math.round(+n || 0);
  const cand = [];
  for (let k = 0; k < 24; k++) cand.push(GRID_BASE + GRID_STEP * k);
  return cand.reduce((best, g) => {
    const d = Math.abs(g - n), bd = Math.abs(best - n);
    return d < bd || (d === bd && g > best) ? g : best;
  }, cand[0]);
}

/** 每镜与前镜的重叠帧（首镜 0）—— 与后端 timing() 相同 */
export function overlaps(segs) {
  const ov = [];
  let prevEnd = 0;
  for (const s of segs) {
    ov.push(ov.length ? Math.max(0, prevEnd - s.startFrame) : 0);
    prevEnd = s.endFrame;
  }
  return ov;
}

/** 时序摘要（总帧 / 段间引导），供 Workbench 预览与提交前校验展示 */
export function timingInfo(segs, fps) {
  const total = segs.length ? segs[segs.length - 1].endFrame : 0;
  const ov = overlaps(segs);
  const contOn = ov.some(o => o >= 1);
  return {
    total,
    totalSec: fps ? total / fps : 0,
    contOn,
    contFrames: contOn ? snapContinuity(Math.max(...ov)) : 0,
  };
}

export const Timeline = {
  _raf: 0,
  _pi: -1,                  // 连播当前分镜下标

  fps() { return window.XQ.state.fps(); },

  /** 成片预览数据：最近一个「分镜数与当前一致且全部分镜视频已生成」的
      完成任务（含每段视频地址与帧数）。视频未生成 / 分镜数不符时为
      null，时间轴按默认（帧布局）状态展示 */
  async loadDonePreview() {
    const S = window.XQ.state;
    const n = S.segs.length;
    if (!n) { S.donePreview = null; return; }
    try {
      const d = await API.get('/api/tasks');
      const hit = (d.tasks || [])
        .filter(t => t.state === 'done' &&
          ((t.config || {}).segmentCount || (t.segments || []).length) === n)
        .sort((a, b) => (b.created_ms || 0) - (a.created_ms || 0))[0];
      if (!hit) { S.donePreview = null; return; }
      const det = await API.get('/api/tasks/' + encodeURIComponent(hit.id));
      const segs = ((det.task || {}).segments || []);
      if (segs.length !== n || segs.some(s => !s.url)) {
        S.donePreview = null; return;
      }
      S.donePreview = {
        id: hit.id, name: hit.name || '',
        segs: segs.map(s => ({ url: s.url, frames: +s.frames || 0 })),
      };
    } catch { S.donePreview = null; }
  },

  render() {
    const S = window.XQ.state;
    const tl = el('tl');
    const segs = S.segs;
    // 保留 ruler，清除旧段
    tl.querySelectorAll('.seg').forEach(n => n.remove());

    // 成片模式：全部分镜视频均已生成 → 按实际视频时长展示、点击即播；
    // 未生成 / 分镜数不符时保持默认帧布局状态
    const pv = (S.donePreview && S.donePreview.segs.length === segs.length)
      ? S.donePreview : null;
    const fps = Timeline.fps();
    if (pv) {
      Timeline._renderMedia(pv, segs, fps);
      return;
    }
    el('tlPlayAll') && el('tlPlayAll').classList.add('hide');

    const total = Math.max(1, segs.length ? segs[segs.length - 1].endFrame : 1);
    segs.forEach((s, i) => {
      const d = document.createElement('div');
      d.className = 'seg' + (i === S.activeSeg ? ' on' : '');
      d.style.left = (s.startFrame / total * 100) + '%';
      d.style.width = (Math.max(0.5, (s.endFrame - s.startFrame)) / total * 100) + '%';
      const durF = s.endFrame - s.startFrame;
      d.innerHTML = `<div class="body">${esc(s.prompt.slice(0, 26) || '分镜 ' + (i + 1))}
        <br>${fmtFrames(s.startFrame, s.endFrame)} · ${fmtSec(durF / fps)}</div>
        <div class="grip" data-side="l"></div><div class="grip" data-side="r"></div>`;
      d.querySelector('.grip[data-side="l"]').style.order = -1;
      d.addEventListener('mousedown', e => Timeline._down(e, i));
      tl.appendChild(d);
    });

    // 标尺刻度（约每 8 格）
    const ruler = el('tlRuler');
    ruler.innerHTML = '';
    const step = niceStep(total);
    for (let f = 0; f <= total; f += step) {
      const sp = document.createElement('span');
      sp.style.left = (f / total * 100) + '%';
      sp.textContent = f;
      ruler.appendChild(sp);
    }

    const info = timingInfo(segs, fps);
    // 自动首尾帧续写：多段自动开启，单段恒关闭（开关状态回填到时间轴头部）
    const cont = window.Segments ? window.Segments.contOn() : info.contOn;
    const sw = el('contSw');
    if (sw) {
      sw.checked = cont;
      sw.disabled = segs.length < 2;
      sw.title = segs.length < 2
        ? '单个分镜时无需首尾帧续写'
        : (cont ? '已启用：下一分镜承接上一分镜尾帧（分镜间引导 '
            + info.contFrames + '帧）' : '已关闭：各分镜独立生成');
    }
    el('tlInfo').textContent = segs.length
      ? `共 ${segs.length} 个分镜 · 总长 ${info.total}帧（${fmtSec(info.totalSec)} @${fps}fps）` +
        (cont && info.contOn ? ` · 分镜间引导 ${info.contFrames}帧` : ' · 分镜独立生成')
      : '暂无分镜';
  },

  /** 成片模式渲染：分镜块宽度按实际视频帧长占比（只读），点击即连播 */
  _renderMedia(pv, segs, fps) {
    const S = window.XQ.state;
    const tl = el('tl');
    const lens = pv.segs.map(s => Math.max(1, +s.frames || 1));
    const total = lens.reduce((a, b) => a + b, 0);
    let cursor = 0;
    pv.segs.forEach((sv, i) => {
      const d = document.createElement('div');
      d.className = 'seg media' + (i === S.activeSeg ? ' on' : '');
      d.style.left = (cursor / total * 100) + '%';
      d.style.width = (lens[i] / total * 100) + '%';
      cursor += lens[i];
      d.innerHTML = `<div class="body">${esc(String(
        (segs[i] || {}).prompt || '').slice(0, 20) || '分镜 ' + (i + 1))}
        <br>成片 ${fmtSec(lens[i] / fps)} · ${icon('play', 11)}播放</div>`;
      d.title = '点击播放该分镜（并按时间轴顺序连播到结尾）';
      d.addEventListener('click', () => Timeline.playFrom(i));
      tl.appendChild(d);
    });
    const ruler = el('tlRuler');
    ruler.innerHTML = '';
    const step = niceStep(total);
    for (let f = 0; f <= total; f += step) {
      const sp = document.createElement('span');
      sp.style.left = (f / total * 100) + '%';
      sp.textContent = f;
      ruler.appendChild(sp);
    }
    const sw = el('contSw');
    if (sw) sw.disabled = segs.length < 2;
    el('tlInfo').textContent =
      `成片预览 · ${pv.segs.length} 个分镜 · 实际总长 ${fmtSec(total / fps)}` +
      ' · 点击分镜块播放';
    el('tlPlayAll') && el('tlPlayAll').classList.remove('hide');
  },

  /** 从第 i 个分镜开始，按时间轴顺序依次播放（至最后一个分镜结束） */
  playFrom(i) {
    const S = window.XQ.state;
    const pv = (S.donePreview && S.donePreview.segs.length === S.segs.length)
      ? S.donePreview : null;
    if (!pv) return toast('分镜视频尚未全部生成，无法播放');
    const box = el('tlPlayer'), v = el('tlpVideo');
    if (!box || !v) return;
    box.classList.remove('hide');
    this._pi = i;
    v.onended = () => {
      if (Timeline._pi + 1 < pv.segs.length) Timeline.playFrom(Timeline._pi + 1);
      else Timeline.closePlayer();
    };
    v.src = pv.segs[i].url;
    v.play().catch(() => { /* 自动播放被浏览器拦截时由用户手动开始 */ });
    el('tlpTitle').textContent = `成片预览` +
      (pv.name ? ` · ${pv.name}` : '') +
      ` · 分镜 ${i + 1}/${pv.segs.length}`;
    el('tlpSegs').innerHTML = pv.segs.map((s, k) =>
      `<span class="tlp-seg${k === i ? ' on' : ''}"
        onclick="Timeline.playFrom(${k})">分镜 ${k + 1}</span>`).join('');
  },

  closePlayer() {
    const v = el('tlpVideo');
    if (v) { v.pause(); v.removeAttribute('src'); v.load(); }
    this._pi = -1;
    el('tlPlayer').classList.add('hide');
  },

  /** 鼠标按下段体/grip：段体点击选中，grip 进入拖拽 */
  _down(e, idx) {
    e.preventDefault();
    const S = window.XQ.state;
    const grip = e.target.classList.contains('grip') ? e.target : null;
    if (!grip) {
      if (S.activeSeg !== idx) {
        S.activeSeg = idx;
        window.Segments.renderAll();
      }
      return;
    }
    const side = grip.dataset.side;
    grip.closest('.seg').classList.add('dragging');
    const tlRect = el('tl').getBoundingClientRect();
    const total = Math.max(1, S.segs[S.segs.length - 1].endFrame);

    const move = (ev) => {
      const x = ev.clientX - tlRect.left;
      let f = snapFrames(Math.round(x / tlRect.width * total));
      const s = S.segs[idx];
      const prev = S.segs[idx - 1], next = S.segs[idx + 1];
      if (side === 'l') {
        const lo = prev ? prev.startFrame + 22 : 0;
        const hi = s.endFrame - GRID_BASE;
        s.startFrame = Math.max(lo, Math.min(hi, f));
      } else {
        const lo = s.startFrame + GRID_BASE;
        const hi = next ? next.endFrame - 22 : 1e9;
        s.endFrame = Math.max(lo, Math.min(hi, f));
      }
      cancelAnimationFrame(Timeline._raf);
      Timeline._raf = requestAnimationFrame(() => {
        Timeline.render();
        window.Segments.refreshMeta(idx);
      });
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      document.querySelectorAll('.seg.dragging').forEach(n =>
        n.classList.remove('dragging'));
      window.Workbench && window.Workbench.autosaveSoon();
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  },
};

function niceStep(total) {
  const steps = [17, 34, 85, 170, 340, 850, 1700];
  return steps.find(s => total / s <= 12) || 3400;
}
