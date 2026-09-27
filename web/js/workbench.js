// workbench.js —— 工作台：参数状态化（本地/云端独立记忆）/ 模型清单 /
// 配置收集与校验 / 任务提交（按分镜粒度）/ 工作流预览 / 预设与自动存档（window.Workbench）
//
// 生成参数不再依赖侧栏 DOM：以 P = {local:{…}, cloud:{…}} 按模式独立记忆，
// 表单在生成模式弹窗（floatpanel.js）内渲染，输入即写回状态并自动存档；
// 提交 / 预览 / 预设始终使用「当前模式」的参数集。

import { el, esc, icon, toast, debounce, fmtClock, fmtSec } from './core.js';
import { API } from './api.js';

// 输入 id → cfg 键映射（数值型 / 下拉与文本 / 复选）
const NUM = {
  width: 'width', height: 'height', fps: 'fps', refMax: 'ref_max_size',
  steps: 'steps', seed: 'seed', lstr: 'lora_strength',
  turboSteps: 'turbo_steps', spSteps: 'secondPassHighSteps',
};
const SEL = {
  unet: 'unet', clip: 'clip', vvae: 'video_vae', avae: 'audio_vae',
  lora: 'lora', sampler: 'sampler', scheduler: 'scheduler',
  spModel: 'secondPassModel', prefix: 'filename_prefix', saveCodec: 'save_codec',
};

const P_KEY = 'xqwui.params';

// 采样器 / 调度器回退选项：引擎离线或 /object_info 无数据时保证弹窗可用；
// 引擎可达时一律使用后端返回的真实列表（/api/models 的 options 字段）
const FALLBACK_OPTS = {
  samplers: ['euler', 'euler_ancestral', 'res_multistep'],
  schedulers: ['simple', 'normal', 'beta', 'karras'],
};

// 分辨率预设（均对齐 32 的倍数，H3 约束）；custom = 跟随宽度/高度输入框
const RES_PRESETS = [
  ['1344x768', '1344 × 768（16:9 横屏）'],
  ['1536x864', '1536 × 864（16:9 高清）'],
  ['1024x576', '1024 × 576（16:9 轻量）'],
  ['768x1344', '768 × 1344（9:16 竖屏）'],
  ['864x1536', '864 × 1536（9:16 竖屏高清）'],
  ['960x960', '960 × 960（1:1 方形）'],
];
const RES_CUSTOM = 'custom';
const align32 = v => Math.max(32, Math.round((parseFloat(v) || 0) / 32) * 32);

function defParams() {
  return {
    width: 1344, height: 768, fps: 24, ref_max_size: 864,
    unet: null, clip: null, video_vae: null, audio_vae: null,
    turboMode: true, lora: null, lora_strength: 1, turbo_steps: 8,
    steps: 8, seed: -1, sampler: 'euler', scheduler: 'simple',
    secondPass: false, secondPassModel: null, secondPassHighSteps: 4,
    filename_prefix: 'xqwui/video', save_codec: 'h264',
  };
}

/** 读取本地/云端双份参数记忆（默认值 + localStorage 覆盖） */
function loadParams() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(P_KEY)) || null; } catch { /* 损坏即弃 */ }
  const P = { local: defParams(), cloud: defParams() };
  for (const k of ['local', 'cloud']) {
    const s = saved && saved[k];
    if (s && typeof s === 'object' && !Array.isArray(s)) {
      // 引擎 COMBO 无 h265：纠正旧记忆值，避免下拉与提交值不一致
      if (s.save_codec && !['h264', 'av1'].includes(s.save_codec)) {
        s.save_codec = 'h264';
      }
      Object.assign(P[k], s);
    }
  }
  return P;
}

function saveParams() {
  try { localStorage.setItem(P_KEY, JSON.stringify(Workbench.P)); } catch { /* 隐私模式忽略 */ }
}

export const Workbench = {
  P: loadParams(),

  /** 当前生成模式（与 floatpanel.js 的 GenMode 保持同源） */
  cur() { return window.XQ.state.genMode === 'cloud' ? 'cloud' : 'local'; },
  /** 当前模式参数集（提交 / 预览 / 预设均以此为准） */
  params() { return Workbench.P[Workbench.cur()]; },

  // ---------------- 模型清单（按模式实时获取） ----------------
  /** kind 缺省取当前模式；cloud 时走云端引擎地址（后端 src=cloud）；
      url 显式指定引擎地址（测试连接成功后按该地址加载，未保存也生效）。
      同时取回引擎真实采样器/调度器选项（options 字段） */
  async loadModels(kind, url) {
    kind = kind || Workbench.cur();
    const qs = [];
    if (kind === 'cloud' && !url) qs.push('src=cloud');
    if (url) qs.push('url=' + encodeURIComponent(url));
    const d = await API.soft(
      API.get('/api/models' + (qs.length ? '?' + qs.join('&') : '')), '模型清单');
    if (!d) return false;
    window.XQ.state.models = d.models || {};
    window.XQ.state.engineOptions = d.options || {};
    Workbench.ensureDefaults('local');
    Workbench.ensureDefaults('cloud');
    Workbench.refreshSelects(kind);
    return true;
  },

  /** 按偏好挑选默认模型 */
  _pick(names, preferRe, fallbackIncludes = '') {
    names = Array.isArray(names) ? names : [];
    return names.find(n => preferRe.test(n)) ||
      (fallbackIncludes && names.find(n => n.includes(fallbackIncludes))) ||
      names[0] || '';
  },

  /** 未曾设置过的模型字段按偏好补默认值（显式「不使用」的空串不覆盖） */
  ensureDefaults(kind) {
    const p = Workbench.P[kind], M = window.XQ.state.models || {};
    const seed = (key, names, preferRe, fb = '') => {
      if (p[key] == null) p[key] = Workbench._pick(names, preferRe, fb);
    };
    seed('unet', M.diffusion_models, /minimax.*h3|h3.*minimax/i, 'mini_max_h3');
    seed('clip', M.text_encoders, /qwen/i);
    seed('video_vae', M.vae, /video/i);
    seed('audio_vae', M.vae, /audio/i);
    seed('lora', M.loras, /turbo|加速/i);
    seed('secondPassModel', M.latent_upscale_models, /latent|upscale/i);
    saveParams();
  },

  _opts(names, empty) {
    names = Array.isArray(names) ? names : [];
    return (empty ? '<option value="">（不使用）</option>' : '') +
      names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
  },

  /** 弹窗展开中且属于该模式时，重填模型下拉（保留有效已选值，否则按偏好补默认） */
  refreshSelects(kind) {
    const box = Workbench._modalBox();
    if (!box || box.dataset.kind !== kind) return;
    const p = Workbench.P[kind], M = window.XQ.state.models || {};
    const O = window.XQ.state.engineOptions || {};
    const samplers = (O.samplers && O.samplers.length)
      ? O.samplers : FALLBACK_OPTS.samplers;
    const schedulers = (O.schedulers && O.schedulers.length)
      ? O.schedulers : FALLBACK_OPTS.schedulers;
    const apply = (id, key, names, preferRe, fb = '', empty = false) => {
      names = Array.isArray(names) ? names : [];
      let v = p[key];
      if (!((v === '' && empty) || (v != null && names.includes(v)))) {
        v = Workbench._pick(names, preferRe, fb) || (empty ? '' : (names[0] || ''));
        p[key] = v;
        saveParams();
      }
      const s = el(id);
      if (s) { s.innerHTML = Workbench._opts(names, empty); s.value = v; }
    };
    apply('unet', 'unet', M.diffusion_models, /minimax.*h3|h3.*minimax/i, 'mini_max_h3');
    apply('clip', 'clip', M.text_encoders, /qwen/i);
    apply('vvae', 'video_vae', M.vae, /video/i);
    apply('avae', 'audio_vae', M.vae, /audio/i);
    apply('lora', 'lora', M.loras, /turbo|加速/i, '', true);
    apply('spModel', 'secondPassModel', M.latent_upscale_models, /latent|upscale/i);
    apply('sampler', 'sampler', samplers, /^euler$/i);
    apply('scheduler', 'scheduler', schedulers, /^simple$/i);
  },

  /** 当前打开的模式弹窗 box（未打开返回 null） */
  _modalBox() {
    const m = el('modalCfg');
    if (!m || !m.classList.contains('on')) return null;
    return m.querySelector('.box');
  },

  // ---------------- 生成参数表单（弹窗内渲染） ----------------
  paramsHtml(kind) {
    const cloudNote = kind === 'cloud'
      ? '<div class="note" style="margin:2px 0 6px">下拉当前展示本地引擎模型清单；' +
        '配置并保存云端地址后，重新打开弹窗将实时获取云端引擎的模型清单。</div>' : '';
    return `
      <div class="psec-h">画面</div>
      <label>分辨率预设</label><select id="resPreset"></select>
      <div class="row">
        <div><label>宽度</label><input type="number" id="width" step="32" min="32"></div>
        <div><label>高度</label><input type="number" id="height" step="32" min="32"></div>
      </div>
      <div class="note" style="margin:2px 0 6px">分辨率支持自定义：预设一键填入，也可直接修改宽度 / 高度（自动对齐 32 的倍数）</div>
      <div class="row">
        <div><label>帧率</label><input type="number" id="fps" step="1" min="1"></div>
        <div><label>参考图长边上限</label><input type="number" id="refMax" step="32"></div>
      </div>
      <div class="psec-h">模型</div>
      ${cloudNote}
      <label>扩散模型</label><select id="unet"></select>
      <label>文本编码器</label><select id="clip"></select>
      <div class="row">
        <div><label>视频 VAE</label><select id="vvae"></select></div>
        <div><label>音频 VAE</label><select id="avae"></select></div>
      </div>
      <label style="margin-top:6px">
        <input type="checkbox" id="turboMode" style="width:auto">
        <b>Turbo mode</b> <span class="note">低步数加速（自动换加速 LoRA）</span>
      </label>
      <label>加速 LoRA（留空=不用）</label><select id="lora"></select>
      <div class="row">
        <div><label>LoRA 强度</label><input type="number" id="lstr" step="0.05"></div>
        <div><label>Turbo 步数</label><input type="number" id="turboSteps" step="1" min="1"></div>
      </div>
      <div class="psec-h">采样</div>
      <div class="row">
        <div><label>步数</label><input type="number" id="steps" step="1" min="1"></div>
        <div><label>随机种子（-1 随机）</label><input type="number" id="seed" step="1"></div>
      </div>
      <div class="row">
        <div><label>采样器</label><select id="sampler"></select></div>
        <div><label>调度器</label><select id="scheduler"></select></div>
      </div>
      <div class="psec-h">二采（SelfLift 高清精修）</div>
      <label class="ck"><input type="checkbox" id="secondPass"> 启用二采（额外一次放大重采样，更慢更清晰）</label>
      <div id="spBox" class="hide">
        <label>latent 放大模型</label><select id="spModel"></select>
        <div class="row"><div><label>高清步数</label>
          <input type="number" id="spSteps" step="1" min="1" max="32"></div></div>
      </div>
      <div class="psec-h">输出</div>
      <div class="row">
        <div><label>文件名前缀</label><input id="prefix"></div>
        <div><label>编码</label><select id="saveCodec">
          <option value="h264">h264</option>
          <option value="av1">av1</option></select></div>
      </div>
      <div class="note" style="margin-top:8px">参数即改即生效（自动存档）；提交任务时使用「${kind === 'cloud' ? '云端' : '本地'}」模式参数集</div>`;
  },

  /** 绑定弹窗内生成参数表单：值回填 + 输入写回状态 + 联动 + 自动存档 */
  bindParams(kind) {
    const box = Workbench._modalBox();
    if (!box) return;
    box.dataset.kind = kind;
    const p = Workbench.P[kind];
    const setv = (id, v) => { const n = el(id); if (n) n.value = v ?? ''; };
    for (const [id, key] of Object.entries(NUM)) setv(id, p[key]);
    for (const [id, key] of Object.entries(SEL)) setv(id, p[key]);
    Workbench._initResPreset(p);
    el('turboMode').checked = !!p.turboMode;
    el('secondPass').checked = !!p.secondPass;
    Workbench.refreshSelects(kind);
    Workbench.toggleSecondPass();
    Workbench._turboUI();
    box.querySelector('#paramsWrap').querySelectorAll('input, select')
      .forEach(n => {
      const evt = (n.type === 'checkbox' || n.tagName === 'SELECT') ? 'change' : 'input';
      n.addEventListener(evt, () => {
        const id = n.id;
        if (id === 'resPreset') {
          Workbench._applyResPreset(n.value);
        } else if (NUM[id]) {
          const v = parseFloat(n.value);
          if (!Number.isNaN(v)) p[NUM[id]] = v;
        } else if (SEL[id]) {
          p[SEL[id]] = n.value.trim();
        } else if (id === 'turboMode' || id === 'secondPass') {
          p[id] = n.checked;
        }
        saveParams();
        if (id === 'turboMode') {
          // 勾选 Turbo 时自动挑加速 LoRA
          if (n.checked && !p.lora) {
            const turbo = (window.XQ.state.models.loras || [])
              .find(x => /turbo|加速/i.test(x));
            if (turbo) {
              p.lora = turbo;
              const s = el('lora');
              if (s) s.value = turbo;
            }
          }
          Workbench._turboUI();
        }
        if (id === 'lora') Workbench._turboUI();
        if (id === 'secondPass') Workbench.toggleSecondPass();
        if (id === 'fps') window.Segments.renderAll();
        Workbench.autosaveSoon();
      });
    });
    // 宽 / 高手动输入完成（change）后对齐 32 的倍数，并同步预设下拉回显
    ['width', 'height'].forEach(id => {
      const n = el(id);
      if (!n) return;
      n.addEventListener('change', () => {
        const a = align32(n.value);
        n.value = a;
        p[NUM[id]] = a;
        saveParams();
        const s = el('resPreset');
        if (s && !RES_PRESETS.some(([v]) => v === `${p.width}x${p.height}`))
          s.value = RES_CUSTOM;
        Workbench.autosaveSoon();
      });
    });
  },

  /** 初始化分辨率预设下拉：填充选项并按当前宽高回显（未命中 → 自定义） */
  _initResPreset(p) {
    const s = el('resPreset');
    if (!s) return;
    s.innerHTML = RES_PRESETS.map(([v, label]) =>
      `<option value="${v}">${esc(label)}</option>`).join('') +
      `<option value="${RES_CUSTOM}">自定义</option>`;
    const hit = RES_PRESETS.find(([v]) => v === `${p.width}x${p.height}`);
    s.value = hit ? hit[0] : RES_CUSTOM;
  },

  /** 应用分辨率预设：填入宽度 / 高度输入框并写回参数状态 */
  _applyResPreset(v) {
    if (v === RES_CUSTOM) return;
    const [w, h] = String(v).split('x').map(align32);
    if (!w || !h) return;
    const p = Workbench.params();
    p.width = w; p.height = h;
    const iw = el('width'), ih = el('height');
    if (iw) iw.value = w;
    if (ih) ih.value = h;
    saveParams();
    Workbench.autosaveSoon();
  },

  /** Turbo 联动 UI：勾选时步数切换、LoRA 强度按可用性启停 */
  _turboUI() {
    const t = el('turboMode');
    if (!t) return;
    const on = t.checked;
    el('lstr').disabled = !on || !el('lora').value;
    el('turboSteps').disabled = !on;
    el('steps').disabled = on;
  },

  /** 二采显隐联动（弹窗内） */
  toggleSecondPass() {
    const cb = el('secondPass');
    if (!cb) return;
    el('spBox').classList.toggle('hide', !cb.checked);
    el('spModel').disabled = !cb.checked;
    el('spSteps').disabled = !cb.checked;
  },

  // ---------------- 配置收集 ----------------
  /** 组装完整任务配置（当前模式参数 + 全局提示词 + 分镜）；validate=true 校验。
      参考素材为项目分镜全局通用池（S.refs）：写入每个分镜（后端契约不变），
      图片不限数量 */
  collect(validate = false) {
    const S = window.XQ.state;
    const refs = window.Media ? window.Media.ensureRefs()
      : { images: [], refVideos: [], refAudios: [] };
    const p = Workbench.params();
    const cfg = {
      ...p,
      globalPrompt: el('gprompt').value.trim(),
      segments: S.segs.map(s => ({
        startFrame: s.startFrame, endFrame: s.endFrame,
        prompt: String(s.prompt || '').trim(),
        images: [...refs.images], refVideos: [...refs.refVideos],
        refAudios: [...refs.refAudios],
        firstFrame: [...(s.firstFrame || [])],
        lastFrame: [...(s.lastFrame || [])],
      })),
      images: [...new Set(refs.images)].map(k => ({ id: k, file: k })),
    };
    // 自动首尾帧续写（段间引导）：前端开关为权威，缺省时后端按帧重叠判定
    cfg.continuity = window.Segments ? !!window.Segments.contOn() : undefined;
    cfg.unet = p.unet || '';
    cfg.clip = p.clip || '';
    cfg.video_vae = p.video_vae || '';
    cfg.audio_vae = p.audio_vae || '';
    cfg.secondPassModel = p.secondPassModel || '';
    if (p.turboMode) {          // turbo：换加速 LoRA + 低步数
      cfg.lora = p.lora || '';
      cfg.steps = parseInt(p.turbo_steps, 10) || 8;
    } else {
      cfg.lora = '';
    }
    cfg.save_format = 'mp4';

    if (validate) {
      const err = Workbench._validate(cfg);
      if (err) { toast(err, 'err'); return null; }
    }
    return cfg;
  },

  /** 提交前校验所选模型是否真实存在于引擎清单（替代引擎 400 原始报错） */
  _missingModels(cfg) {
    const M = window.XQ.state.models || {};
    const kinds = ['diffusion_models', 'text_encoders', 'vae', 'loras',
                   'latent_upscale_models'];
    if (!kinds.some(k => (M[k] || []).length)) return null;  // 未拉取过清单
    const has = (k, name) => !name || (M[k] || []).includes(name);
    const bad = [];
    if (!has('diffusion_models', cfg.unet)) bad.push('扩散模型 ' + cfg.unet);
    if (!has('text_encoders', cfg.clip)) bad.push('文本编码器 ' + cfg.clip);
    if (!has('vae', cfg.video_vae)) bad.push('视频 VAE ' + cfg.video_vae);
    if (!has('vae', cfg.audio_vae)) bad.push('音频 VAE ' + cfg.audio_vae);
    if (cfg.secondPass && !has('latent_upscale_models', cfg.secondPassModel))
      bad.push('latent 放大模型 ' + cfg.secondPassModel);
    if (cfg.lora && !has('loras', cfg.lora)) bad.push('加速 LoRA ' + cfg.lora);
    if (!bad.length) return null;
    if (!['diffusion_models', 'text_encoders', 'vae'].some(k => (M[k] || []).length))
      return '引擎未发现任何模型文件：请检查 ComfyUI 模型目录 / extra_model_paths 配置并重启引擎';
    return '引擎不存在以下模型，请在生成参数弹窗中重新选择：' + bad.join('、');
  },

  /** 客户端前置校验（服务端仍会做权威校验）；offset 用于单分镜任务回显真实镜号 */
  _validate(cfg, offset = 0) {
    if (!cfg.segments.length) return '请先添加分镜';
    if (!cfg.unet) return '请选择扩散模型（右侧模式按钮 → 本地/云端生成参数）';
    if (!cfg.clip) return '请选择文本编码器';
    const miss = Workbench._missingModels(cfg);
    if (miss) return miss;
    for (let i = 0; i < cfg.segments.length; i++) {
      const s = cfg.segments[i];
      const tag = `分镜 ${i + 1 + offset}`;
      if (s.endFrame <= s.startFrame) return `${tag}：结束帧必须大于起始帧`;
      // 仅含 @素材: 引用、无实际画面描述时视为空提示词
      if (!s.prompt.replace(/@素材:[^\s@，。；、,;]+/g, '').trim())
        return `${tag}：提示词为空（媒体引用不能替代画面描述）`;
      if (!s.images.length && !(s.firstFrame || []).length) {
        const inherited = cfg.segments.slice(0, i)
          .some(x => x.images.length || (x.firstFrame || []).length);
        if (!inherited) return `${tag}：首镜必须指定参考图或首帧素材（inherit 链起点）`;
      }
    }
    if (cfg.secondPass && !cfg.secondPassModel)
      return '二采已开启但未选择 latent 放大模型';
    return null;
  },

  // ---------------- 提交 / 预览 ----------------
  // 提交粒度 = 分镜：
  //   单个生成 —— 只为当前激活分镜提交 1 个视频任务
  //   批量生成 —— 按分镜顺序为所有分镜各提交任务（可每镜多份变体）

  /** 第 i 个分镜 → 独立任务配置（其余参数沿用当前工作台配置） */
  _soleCfg(cfg, i) {
    const s = cfg.segments[i];
    const seg = { ...s };
    // inherit 链解析：单分镜任务无法跨任务继承，本镜无参考图时沿用上一镜
    // （与后端 ref_files_of 同语义：优先上一镜参考图，其次其首帧）
    if (!(seg.images || []).length) {
      for (let k = i - 1; k >= 0; k--) {
        const up = cfg.segments[k];
        const refs = (up.images || []).length ? [...up.images]
          : [...(up.firstFrame || [])];
        if (refs.length) { seg.images = refs; break; }
      }
    }
    const one = { ...cfg, segments: [seg] };
    one.images = [...new Set([...(seg.images || []),
      ...(seg.firstFrame || []), ...(seg.lastFrame || [])])]
      .map(k => ({ id: k, file: k }));
    one.continuity = false;          // 单分镜任务无分镜间引导
    return one;
  },

  /** 任务名：前缀 + 分镜号 + 变体号 + 提示词摘要 */
  _segName(i, prompt, prefix = '', variant = 0) {
    const parts = [prefix, `分镜 ${i + 1}`];
    if (variant) parts.push('#' + variant);
    const p = String(prompt || '').trim();
    if (p) parts.push('· ' + p.slice(0, 20));
    return parts.filter(Boolean).join(' ');
  },

  /** 单个生成：只为当前激活分镜提交一个视频任务 */
  async submit() {
    const cfg = Workbench.collect(false);
    if (!cfg) return;
    const i = window.XQ.state.activeSeg | 0;
    if (!cfg.segments[i]) { toast('请先添加分镜', 'err'); return; }
    const one = Workbench._soleCfg(cfg, i);
    const err = Workbench._validate(one, i);
    if (err) { toast(err, 'err'); return; }
    const name = Workbench._segName(i, one.segments[0].prompt,
      cfg.globalPrompt ? cfg.globalPrompt.slice(0, 16) : '');
    const btn = el('subBar');
    btn.classList.remove('hide');
    btn.innerHTML = '<i style="width:40%"></i>';
    try {
      const r = await API.post('/api/tasks', { name, config: one });
      toast(`分镜 ${i + 1} 任务已提交：${r.task?.id || ''}`, 'ok');
      window.UI.openTasks();
    } catch (e) {
      toast('提交失败：' + e.message, 'err');
    } finally {
      btn.classList.add('hide');
    }
  },

  /** 批量生成弹窗：每镜份数 / 种子策略 / 名称前缀 */
  async openBatch() {
    const n = (window.XQ.state.segs || []).length;
    window.UI.showModal('modalBatch',
      `<h3>${icon('stack')}批量生成</h3>
       <div class="env">
         <div class="kv"><b>分镜数</b><span>${n} 个（按分镜顺序逐个提交）</span></div>
         <div class="kv"><b>每镜份数</b><span><input id="bCount" type="number" min="1" max="50" value="1" style="width:70px"></span></div>
         <div class="kv"><b>种子策略</b><span><select id="bSeed">
           <option value="random">随机种子 —— 每份独立随机</option>
           <option value="inc">递增种子 —— 基准种子 + 序号</option>
           <option value="keep">固定种子 —— 完全一致（仅名称不同）</option>
         </select></span></div>
         <div class="kv"><b>名称前缀</b><span><input id="bPrefix" placeholder="留空自动命名" style="width:220px"></span></div>
       </div>
       <div class="note" style="margin:10px 0">为每个分镜分别提交一个视频任务（共 ${n} × 每镜份数 个任务，上限 50）；批量计入「设置 → 队列上限」（active + 任务数超限时整批拒绝，可调高上限或分批）。完成后可在「任务 → 批次」查看批处理报告。</div>
       <div style="display:flex;gap:9px;justify-content:flex-end">
         <button class="primary" onclick="Workbench.submitBatch()">${icon('bolt', 13)}提交批量</button>
       </div>`);
  },

  async submitBatch() {
    const cfg = Workbench.collect(true);
    if (!cfg) return;
    const count = Math.max(1, Math.min(50, +el('bCount').value || 1));
    const strategy = el('bSeed').value;
    const prefix = (el('bPrefix').value || '').trim();
    if (cfg.segments.length * count > 50) {
      toast(`分镜数 × 每镜份数 = ${cfg.segments.length * count} 超过上限 50，请减少份数`, 'err');
      return;
    }
    window.UI.closeModal('modalBatch');
    let base = parseInt(cfg.seed, 10);
    if (!Number.isFinite(base) || base < 0) base = Math.floor(Math.random() * 4294967296);
    const items = [];
    cfg.segments.forEach((s, i) => {
      for (let k = 0; k < count; k++) {
        const one = Workbench._soleCfg(cfg, i);
        one.seed = strategy === 'random' ? -1
          : strategy === 'inc' ? (base + i * count + k) % 4294967296 : base;
        items.push({
          name: Workbench._segName(i, s.prompt, prefix, count > 1 ? k + 1 : 0),
          config: one,
        });
      }
    });
    try {
      const r = await API.post('/api/tasks/batch', { items });
      toast(`批量已入队：接受 ${r.accepted} 个` +
        (r.rejected ? `，拒绝 ${r.rejected} 个` : ''), 'ok');
      window.UI.openTasks();
      window.Tasks && window.Tasks.openBatches();
    } catch (e) { toast('批量生成失败：' + e.message, 'err'); }
  },

  /** 工作台「停止任务」按钮显隐（任务中心状态聚合驱动，随 WS 实时刷新） */
  refreshStopBtn(active) {
    const b = document.getElementById('stopBtn');
    if (b) b.classList.toggle('hide', !active);
  },

  /** 停止任务：取消所有排队 / 生成中 / 暂停中的任务（工作台快捷入口）。
      已生成的分镜产物保留在成品库，可稍后在任务中心「重试」续跑 */
  async stopTasks() {
    const act = (window.Tasks && window.Tasks.activeSummaries()) || [];
    if (!act.length) { toast('当前没有进行中的任务', 'err'); return; }
    const names = act.map(t => t.name || t.id).join('、').slice(0, 120);
    if (!confirm(`停止 ${act.length} 个任务？\n${names}\n\n已生成的分镜产物会保留，` +
        `可稍后在任务中心「重试」续跑。`)) return;
    let ok = 0;
    for (const t of act) {
      try {
        const r = await API.post(
          `/api/tasks/${encodeURIComponent(t.id)}/cancel`, {});
        if (r && r.ok) {
          ok++;
          window.Tasks && window.Tasks.upsert(r.task);
        }
      } catch { /* 单个失败继续 */ }
    }
    toast(ok ? `已停止 ${ok}/${act.length} 个任务` : '停止失败：状态不允许',
      ok ? 'ok' : 'err');
  },

  /** 工作流预览：subHint 显示时序，jsonOnly 时写 jsonOut 视图 */
  async preview(jsonOnly = false) {
    const cfg = Workbench.collect(false);
    if (!cfg) return;
    try {
      const r = await API.post('/api/workflow/preview?seg=', { config: cfg });
      const pv = r.preview || {};
      const t = pv.timing || {};
      const hint = `时序 OK · ${pv.nodeCount} 节点 · 总长 ${t.total_frames}帧` +
        `（${fmtSec(t.total_frames / (pv.fps || 24))}）· ` +
        (t.cont_on ? `分镜间引导 ${t.cont_frames}帧` : '分镜独立');
      el('subHint').textContent = hint;
      if (jsonOnly) {
        el('jsonOut').textContent = JSON.stringify({
          config: cfg, preview: pv,
        }, null, 2);
      } else {
        toast('工作流构建成功：' + hint, 'ok');
      }
    } catch (e) {
      el('subHint').textContent = '';
      if (jsonOnly) el('jsonOut').textContent = '构建失败：' + e.message;
      toast('预览失败：' + e.message, 'err');
    }
  },

  // ---------------- 全局状态快照（自动存档用） ----------------
  /** 全局信息：生成模式 / 双模式参数集 / 当前激活分镜 / 全局提示词。
      collect() 只产出「当前模式 + 分镜」的配置，模式与另一模式的参数集
      等全局状态都在 state 与 localStorage 里，必须一并落盘才能完整恢复 */
  globalState() {
    const g = el('gprompt');
    return {
      genMode: Workbench.cur(),
      activeSeg: window.XQ.state.activeSeg || 0,
      globalPrompt: g ? g.value : '',
      params: { local: { ...Workbench.P.local }, cloud: { ...Workbench.P.cloud } },
      saved_ms: Date.now(),
    };
  },

  /** 应用全局状态快照（模式 / 双模式参数 / 激活分镜 / 全局提示词） */
  applyGlobal(g) {
    if (!g || typeof g !== 'object') return false;
    let hit = false;
    const P = g.params;
    if (P && typeof P === 'object') {
      for (const k of ['local', 'cloud']) {
        const src = P[k];
        if (src && typeof src === 'object' && !Array.isArray(src)) {
          Workbench.P[k] = Object.assign(defParams(), src);
          hit = true;
        }
      }
      saveParams();
    }
    if (g.genMode === 'cloud' || g.genMode === 'local') {
      window.GenMode && window.GenMode.setMode(g.genMode);
      hit = true;
    }
    if (Number.isFinite(+g.activeSeg)) {
      window.XQ.state.activeSeg = Math.max(0, parseInt(g.activeSeg, 10) || 0);
      hit = true;
    }
    if (typeof g.globalPrompt === 'string') {
      const n = el('gprompt');
      if (n) { n.value = g.globalPrompt; window.Media.refresh(n); }
      hit = true;
    }
    return hit;
  },

  // ---------------- 预设 / 自动存档 ----------------
  async savePreset() {
    const cfg = Workbench.collect(false);
    if (!cfg) return;
    const name = prompt('方案名称：', '我的方案');
    if (!name || !name.trim()) return;
    try {
      await API.post('/api/presets', { name: name.trim(), config: cfg });
      toast(`方案「${name.trim()}」已保存`, 'ok');
    } catch (e) { toast('保存失败：' + e.message, 'err'); }
  },

  async openPresets() {
    let d;
    try { d = await API.get('/api/presets'); } catch (e) {
      return toast('读取方案失败：' + e.message, 'err');
    }
    const items = d.presets || [];
    UI.showModal('modalPresets', `<h3>${icon('folder')}方案存档</h3>
      <div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap">
        <button class="mini" onclick="Workbench.exportPresets()" title="打包全部方案为 zip 下载">${icon('save', 12)}导出配方包</button>
        <button class="mini" onclick="Workbench.importPresets()" title="从 zip 配方包导入方案">${icon('upload', 12)}导入配方包</button>
        <span class="spacer"></span>
        <button class="mini primary" onclick="Workbench.savePreset()">${icon('check', 12)}保存当前为方案</button>
      </div>
      ${items.length ? items.map(p => `
        <div class="prow">
          <span class="pname">${esc(p.name)}</span>
          <button class="mini" onclick="Workbench._loadPreset('${esc(p.name)}')">${icon('play', 12)}读取</button>
          <button class="mini danger" onclick="Workbench._delPreset('${esc(p.name)}')">${icon('trash', 12)}</button>
        </div>`).join('')
      : '<div class="refempty">暂无方案 —— 点「保存当前为方案」存档</div>'}`);
  },

  /** 配方包导出：POST /api/presets/export（zip 附件下载） */
  async exportPresets() {
    try {
      const r = await fetch('/api/presets/export', { method: 'POST' });
      if (!r.ok) {
        const d = await r.json().catch(() => null);
        throw new Error((d && d.error) || ('HTTP ' + r.status));
      }
      const blob = await r.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'xqwui-presets.zip';
      a.click();
      URL.revokeObjectURL(a.href);
      toast('配方包已导出', 'ok');
    } catch (e) { toast('导出失败：' + e.message, 'err'); }
  },

  /** 配方包导入：选择 zip → multipart 上传；overwrite=1 覆盖同名 */
  async importPresets() {
    const inp = document.createElement('input');
    inp.type = 'file';
    inp.accept = '.zip,application/zip';
    inp.onchange = async () => {
      const f = inp.files && inp.files[0];
      if (!f) return;
      try {
        const r = await API.upload('/api/presets/import?overwrite=1', f);
        const parts = [];
        if (r.imported?.length) parts.push(`导入 ${r.imported.length} 个`);
        if (r.skipped?.length) parts.push(`跳过同名 ${r.skipped.length} 个`);
        if (r.failed?.length)
          parts.push(`失败 ${r.failed.length} 个（${r.failed.map(x => x.name).join('、')}）`);
        toast('配方包导入完成：' + (parts.join('，') || '无可导入项'),
          r.failed?.length ? 'err' : 'ok');
        Workbench.openPresets();
      } catch (e) { toast('导入失败：' + e.message, 'err'); }
    };
    inp.click();
  },

  async _loadPreset(name) {
    try {
      const d = await API.get('/api/presets/' + encodeURIComponent(name));
      Workbench.applyConfig((d.preset || {}).config || {});
      UI.closeModal('modalPresets');
      toast(`已读取方案「${name}」`, 'ok');
    } catch (e) { toast('读取失败：' + e.message, 'err'); }
  },

  async _delPreset(name) {
    if (!confirm('删除方案「' + name + '」？')) return;
    try {
      await API.del('/api/presets/' + encodeURIComponent(name));
      toast('方案已删除', 'ok');
      Workbench.openPresets();
    } catch (e) { toast('删除失败：' + e.message, 'err'); }
  },

  /** 自动存档（800ms 防抖）：配置 + 全局状态一起落盘（按当前项目隔离） */
  autosaveSoon: null,           // init 时赋值
  _autosaveRaw: async () => {
    const cfg = Workbench.collect(false);
    if (!cfg) return;
    const g = Workbench.globalState();
    window.XQ.config = cfg;
    window.XQ.global = g;
    const pid = window.Projects ? window.Projects.currentId() : '';
    try {
      await API.post('/api/autosave', { project: pid, config: cfg, global: g });
      el('autoSaveHint').textContent = '已自动存档 ' + fmtClock(Date.now());
    } catch { /* 静默：自动存档失败不打扰 */ }
  },

  /** 立即落盘：关闭 / 隐藏页面时兜底，避免防抖窗口内的修改丢失。
      用 sendBeacon 保证请求在页面卸载后仍能送达（失败不影响体验） */
  flushAutosave() {
    const cfg = Workbench.collect(false);
    if (!cfg) return;
    const g = Workbench.globalState();
    window.XQ.config = cfg;
    const pid = window.Projects ? window.Projects.currentId() : '';
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon('/api/autosave', new Blob(
          [JSON.stringify({ project: pid, config: cfg, global: g })],
          { type: 'application/json' }));
      }
    } catch { /* 忽略：兜底保存失败不打扰 */ }
  },

  // ---------------- 配置应用 / 恢复 ----------------
  /** 写入当前模式的参数状态（预设 / 自动存档恢复共用） */
  applyConfig(cfg) {
    if (!cfg || typeof cfg !== 'object') return;
    const S = window.XQ.state;
    const p = Workbench.P[Workbench.cur()];
    const put = (key) => {
      if (cfg[key] !== undefined && cfg[key] !== null) p[key] = cfg[key];
    };
    ['width', 'height', 'fps', 'ref_max_size', 'unet', 'clip', 'video_vae',
      'audio_vae', 'lora', 'lora_strength', 'steps', 'seed', 'sampler',
      'scheduler', 'secondPassModel', 'secondPassHighSteps',
      'filename_prefix', 'save_codec', 'turbo_steps'].forEach(put);
    // 恢复自动首尾帧续写开关（缺省表示「跟随段数自动」）
    if (cfg.continuity !== undefined) window.XQ.state.autoCont = !!cfg.continuity;
    if (cfg.turboMode !== undefined) p.turboMode = !!cfg.turboMode;
    p.secondPass = !!cfg.secondPass;
    saveParams();
    // 仅在存档确实带该字段时覆盖，避免空值抹掉已恢复的全局提示词
    if (typeof cfg.globalPrompt === 'string') {
      const n = el('gprompt');
      if (n) { n.value = cfg.globalPrompt; window.Media.refresh(n); }
    }
    S.segs = (cfg.segments || []).map(s => ({
      startFrame: +s.startFrame || 0, endFrame: +s.endFrame || 124,
      prompt: String(s.prompt || ''),
      firstFrame: [...(s.firstFrame || [])], lastFrame: [...(s.lastFrame || [])],
    }));
    // 参考素材全局池恢复：新存档各分镜携带同一套 refs，取第一镜即可；
    // 旧存档（分镜级独立 refs）→ 各镜并集迁移，保证历史数据不丢
    {
      const refs = window.Media ? window.Media.ensureRefs()
        : null;
      if (refs) {
        const segs = cfg.segments || [];
        const union = (field) => {
          const lists = segs.map(s => [...(s[field] || [])]);
          const same = lists.length &&
            lists.every(l => l.join('\n') === lists[0].join('\n'));
          const out = [];
          for (const l of lists)
            for (const k of l) if (!out.includes(k)) out.push(k);
          refs[field] = same ? lists[0] : out;
        };
        union('images'); union('refVideos'); union('refAudios');
      }
    }
    // 激活分镜按新分镜数收敛（保留自动存档恢复出来的当前段，不强行回到第 1 段）
    S.activeSeg = Math.min(S.activeSeg || 0, Math.max(0, S.segs.length - 1));
    window.Segments.renderAll();
    // 参数弹窗展开且属于当前模式时，整体重渲染并重绑
    const box = Workbench._modalBox();
    if (box && box.dataset.kind === Workbench.cur()) {
      box.querySelector('#paramsWrap').innerHTML =
        Workbench.paramsHtml(Workbench.cur());
      Workbench.bindParams(Workbench.cur());
    }
  },

  /** 恢复自动存档（按当前项目隔离）：先恢复全局状态（模式 / 双模式参数 /
      激活分镜 / 全局提示词），再恢复配置（当前模式参数 + 分镜）。
      只要存档存在即恢复 —— 分镜被清空属于用户的真实编排结果，不能因此
      丢弃全局信息并回退到示例 */
  async restoreAutosave() {
    try {
      const pid = window.Projects ? window.Projects.currentId() : '';
      if (!pid) return false;   // 无项目：不读存档（含旧版兼容存档），保持空白
      const d = await API.get('/api/autosave?project=' +
        encodeURIComponent(pid));
      const a = d.autosave || {};
      const cfg = (a.config && typeof a.config === 'object') ? a.config : null;
      const g = (a.global && typeof a.global === 'object') ? a.global : null;
      const okGlobal = Workbench.applyGlobal(g);
      if (cfg) {
        Workbench.applyConfig(cfg);
        window.XQ.config = cfg;
        return true;
      }
      if (okGlobal) {
        window.Segments && window.Segments.renderAll();
        return true;
      }
    } catch { /* 无存档或服务未就绪 */ }
    return false;
  },

  /** 切换项目：加载目标项目的分镜数据（无存档 → 空白工作台）。
      调用前当前项目的草稿应已落盘（Projects.open 已处理）。 */
  async loadProject(pid) {
    window.XQ.state.currentProject = pid;
    const restored = await Workbench.restoreAutosave();
    if (!restored) window.Segments.blank();
  },

  /** 常驻输入绑定（全局提示词；生成参数随弹窗在 bindParams 中绑定）。
      自动保存触发与分镜提示词保持一致：input / change 都直接交给
      autosaveSoon（800ms 防抖），不再叠加额外延时，避免内容回写滞后 */
  bindInputs() {
    const g = el('gprompt');
    window.Media.enableDrop('gprompt');       // 支持素材卡拖入插入引用
    g.addEventListener('change', () => Workbench.autosaveSoon());
    g.addEventListener('input', () => Workbench.autosaveSoon());
  },
};

Workbench.autosaveSoon = debounce(Workbench._autosaveRaw, 800);
