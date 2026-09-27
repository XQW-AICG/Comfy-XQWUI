// floatpanel.js —— 界面右侧悬浮块：生成模式切换（window.GenMode）
//
// 右侧两枚独立按钮「本地生成 / 云端生成」：
// · 点击即切换当前生成模式，激活按钮高亮，并自动弹出对应模式的参数配置弹窗
// · 每种模式的参数独立记忆（来回切换不丢失），修改实时同步 localStorage，
//   刷新页面后恢复；点「保存」才写入应用配置（POST /api/settings）
// · 「测试连接」→ POST /api/engine/test：成功显示引擎版本 / GPU / 延迟，
//   失败显示明确错误信息；云端引擎尚未接入生成链路（弹窗内有提示）

import { el, esc, icon, toast } from './core.js';
import { API } from './api.js';

const MEM_KEY = 'xqwui.genmode';

// 每种模式的字段定义（id 同时用作记忆键）
const FIELDS = {
  local: [
    { id: 'fpLocalUrl', label: '引擎地址', ph: 'http://127.0.0.1:8189' },
    { id: 'fpLocalToken', label: '访问令牌（可选）', ph: '留空=不鉴权' },
    { id: 'fpLocalRetry', label: '断线重连间隔（秒）', type: 'number' },
  ],
  cloud: [
    { id: 'fpCloudUrl', label: '服务地址', ph: 'https://（云端引擎地址）' },
    { id: 'fpCloudToken', label: '访问令牌（可选）', ph: '' },
  ],
};

const KINDS = {
  local: { url: 'fpLocalUrl', token: 'fpLocalToken', retry: 'fpLocalRetry',
           out: 'fpLocalOut' },
  cloud: { url: 'fpCloudUrl', token: 'fpCloudToken', out: 'fpCloudOut' },
};

const META = {
  local: { title: '本地生成参数', ic: 'bolt',
    note: '使用本地推理引擎（engine/）执行生成；地址与令牌保存后立即生效。' },
  cloud: { title: '云端生成参数', ic: 'cloud',
    note: '云端引擎尚未接入生成链路，可先保存服务配置。' },
};

// ---- 状态记忆：{ mode, local:{字段:值}, cloud:{字段:值} } ----
let mem = { mode: 'local', local: {}, cloud: {} };

function loadSaved() {
  try { return JSON.parse(localStorage.getItem(MEM_KEY)) || null; }
  catch { return null; }
}
function persist() {
  try { localStorage.setItem(MEM_KEY, JSON.stringify(mem)); } catch { /* 隐私模式忽略 */ }
}
/** 把弹窗当前输入值收入记忆 */
function capture(kind) {
  (FIELDS[kind] || []).forEach(f => {
    const n = el(f.id);
    if (n) mem[kind][f.id] = n.value;
  });
  persist();
}

export const GenMode = {
  current: () => mem.mode,

  /** 启动装配：后端配置为基线，叠加本地记忆的未保存修改，并恢复激活按钮 */
  async init() {
    const saved = loadSaved();
    const d = await API.soft(API.get('/api/settings'), '引擎设置');
    const s = (d && d.settings) || {};
    mem = {
      mode: (saved && saved.mode === 'cloud') ? 'cloud' : 'local',
      local: Object.assign(
        { fpLocalUrl: s.engine_url || '', fpLocalToken: s.token || '',
          fpLocalRetry: s.engine_retry ?? 3 },
        (saved && saved.local) || {}),
      cloud: Object.assign(
        { fpCloudUrl: s.cloud_engine_url || '', fpCloudToken: s.cloud_engine_token || '' },
        (saved && saved.cloud) || {}),
    };
    mem.local.fpLocalRetry = parseInt(mem.local.fpLocalRetry, 10) || 3;
    persist();
    GenMode.render();
  },

  /** 仅切换模式（不弹窗）：供自动存档恢复使用 */
  setMode(kind) {
    mem.mode = kind === 'cloud' ? 'cloud' : 'local';
    persist();
    GenMode.render();
  },

  /** 切换生成模式：更新高亮 + 弹出对应参数配置弹窗（模式变化立即自动存档） */
  select(kind) {
    GenMode.setMode(kind);
    GenMode.open(kind);
    window.Workbench && window.Workbench.autosaveSoon();
  },

  /** 视觉状态指示：激活按钮高亮 */
  render() {
    window.XQ.state.genMode = mem.mode;
    el('modeLocal').classList.toggle('on', mem.mode === 'local');
    el('modeCloud').classList.toggle('on', mem.mode === 'cloud');
  },

  /** 打开对应模式的参数配置弹窗：引擎连接 + 生成参数（实时获取模型清单） */
  open(kind) {
    const m = META[kind];
    UI.showModal('modalCfg', `
      <h3>${icon(m.ic)}${m.title}</h3>
      <div class="note" style="margin-bottom:4px">${m.note}</div>
      <div class="psec-h">引擎连接</div>
      ${FIELDS[kind].map(f => `
        <label>${f.label}</label>
        <input id="${f.id}"${f.type === 'number' ? ' type="number" min="1"' : ''}
          value="${esc(mem[kind][f.id] ?? '')}" placeholder="${esc(f.ph || '')}">`).join('')}
      <div class="fp-btns">
        <button class="mini" onclick="GenMode.test('${kind}')">测试连接</button>
        <button class="mini primary" onclick="GenMode.save('${kind}')">保存</button>
      </div>
      <div class="fp-out note" id="${KINDS[kind].out}"></div>
      <div id="paramsWrap">${Workbench.paramsHtml(kind)}</div>`);
    FIELDS[kind].forEach(f =>
      el(f.id).addEventListener('input', () => capture(kind)));
    Workbench.bindParams(kind);
    // 实时获取该模式引擎的模型清单（云端未配置地址时跳过，下拉留空）
    const cloudReady = kind !== 'cloud' || (mem[kind].fpCloudUrl || '').trim();
    if (cloudReady) {
      Workbench.loadModels(kind)
        .then(ok => { if (ok) Workbench.refreshSelects(kind); });
    }
  },

  /** 测试连接：成功返回版本信息，失败给明确错误提示 */
  async test(kind) {
    const f = KINDS[kind];
    const url = el(f.url).value.trim();
    const out = el(f.out);
    if (!url) {
      out.innerHTML = '<span class="no">请先填写服务地址</span>';
      return;
    }
    out.textContent = '测试中…';
    let r;
    try {
      r = await API.post('/api/engine/test', { url });
    } catch (e) {
      out.innerHTML = `<span class="no">连接失败：${esc(e.message)}</span>`;
      return;
    }
    if (!r.ok) {
      out.innerHTML = `<span class="no">连接失败：${esc(r.error || '未知错误')}</span>`;
      return;
    }
    const gpu = r.gpu ? ` · ${esc(String(r.gpu).slice(0, 22))}` : '';
    const note = kind === 'cloud'
      ? '<div class="note">可达，但云端引擎尚未接入生成链路</div>' : '';
    out.innerHTML = `<span class="ok">连接成功 · ${esc(r.version || '未知版本')}` +
      ` · ${r.latency_ms}ms${gpu}</span>${note}`;
    // 连接成功 → 按刚测试的地址从后端拉取真实模型清单与采样配置并回填下拉框
    Workbench.loadModels(kind, url).then(ok => {
      if (!ok) return;
      const M = window.XQ.state.models || {};
      const n = k => (M[k] || []).length;
      const O = window.XQ.state.engineOptions || {};
      out.innerHTML += `<div class="note">已加载模型清单：扩散模型 ${n('diffusion_models')}` +
        ` · 文本编码器 ${n('text_encoders')} · VAE ${n('vae')} · LoRA ${n('loras')}` +
        ` · 二次采样 ${n('latent_upscale_models')}` +
        (O.samplers && O.samplers.length
          ? ` · 采样器/调度器选项 ${O.samplers.length}/${(O.schedulers || []).length}` : '') +
        `</div>`;
      Workbench.refreshSelects(kind);
    });
  },

  /** 保存当前模式配置（本地写入主引擎配置；云端写入预留字段） */
  async save(kind) {
    capture(kind);
    const f = KINDS[kind];
    const url = el(f.url).value.trim();
    if (!url) { toast('请先填写服务地址', 'err'); return; }
    const body = kind === 'local'
      ? { engine_url: url, token: el(f.token).value.trim(),
          engine_retry: parseInt(el(f.retry).value, 10) || 3 }
      : { cloud_engine_url: url, cloud_engine_token: el(f.token).value.trim() };
    try {
      await API.post('/api/settings', body);
      toast(kind === 'local' ? '本地生成参数已保存' : '云端生成参数已保存', 'ok');
      el(f.out).innerHTML = kind === 'local'
        ? '<span class="ok">已保存 —— 可点击「测试连接」验证</span>'
        : '<span class="ok">已保存（云端引擎尚未接入）</span>';
      if (kind === 'local') window.pollStatus && window.pollStatus();
    } catch (e) { toast('保存失败：' + e.message, 'err'); }
  },
};
