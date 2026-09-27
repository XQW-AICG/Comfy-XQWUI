// settings.js —— 系统设置：功能模块总览（卡片）+ 各模块专属配置弹窗
//
// 结构：
//   Settings.open()          拉取全部配置数据 → 渲染模块卡片总览
//   Settings.openModule(id)  渲染单个模块的专属表单（复用 modalCfg 容器）
//   Settings.applyModule()   应用当前模块的修改（仅提交该模块的字段）
//   Settings._hub()          返回模块总览
// 模块：
//   storage 存储路径 · queue 队列与磁盘 · log 日志管理 · snap 配置历史
//   engine  生成模式与引擎（转交右侧悬浮块的 GenMode 弹窗）
// 数据统一缓存在 Settings._d，模块间切换即时重绘；操作类按钮（切割 /
// 清理 / 回滚）完成后刷新对应缓存并重绘当前模块。

import { el, esc, icon, toast, fmtTime, fmtBytes } from './core.js';
import { API } from './api.js';
import { Media } from './media.js';

// 日志切割策略：与后端 logutil.WHENS 一致（大小上限 + 时间周期双触发）
const LOG_WHEN = [
  ['off', '仅按大小'],
  ['hourly', '每小时整点'],
  ['midnight', '每日 0 点'],
];

const LIBS = [
  ['materials_images', '图片素材库路径', '参考图 / 首帧 / 尾帧等图片素材的存储位置'],
  ['materials_videos', '视频素材库路径', '参考视频素材的存储位置'],
  ['materials_audios', '音频素材库路径', '参考音频素材的存储位置'],
  ['results', '成品库路径', '生成视频与成片的存储位置'],
];
// 后端 /api/storage 接受的存储库白名单（与服务端 SETTABLE 一致）
const SETTABLE = new Set(LIBS.map(([b]) => b));

// ---------------- 关于系统：i18n 文案字典与更新摘要 ----------------
// 文案一律经 t() 查表（locale 持久化于 localStorage xqwui.locale），
// 扩展新语言只需补一份字典，不改动渲染逻辑。
const LOCALES = {
  zh: {
    version: '版本', build: '构建日期', dev: '开发维护', license: '开源许可',
    support: '支持与反馈', changelog: '版本更新摘要', check: '检查更新',
    copy: '复制版本信息', checking: '检查中…', upToDate: '已是最新版本',
    newVer: '发现新版本', checkFail: '检查失败', python: '运行时',
    copied: '版本信息已复制', copyFail: '复制失败',
    copyHint: '反馈问题时请附上版本信息与日志',
    noHome: '未配置（config.json → homepage）',
  },
  en: {
    version: 'Version', build: 'Build date', dev: 'Developed by',
    license: 'License', support: 'Support', changelog: 'Release notes',
    check: 'Check for updates', copy: 'Copy version info',
    checking: 'Checking…', upToDate: 'You are on the latest version',
    newVer: 'New version available', checkFail: 'Check failed',
    python: 'Runtime', copied: 'Version info copied', copyFail: 'Copy failed',
    copyHint: 'Attach version info and logs when reporting issues',
    noHome: 'Not configured (config.json → homepage)',
  },
};
const loc = () => {
  try { return localStorage.getItem('xqwui.locale') || 'zh'; }
  catch { return 'zh'; }
};
const t = k => ((LOCALES[loc()] || LOCALES.zh)[k]) || LOCALES.zh[k] || k;

// 版本更新摘要（简短，只展示最新一个版本；发布新版本时在数组顶部追加）
const CHANGELOG = {
  zh: [{
    v: '0.0.1', date: '2026-09-27', items: [
      '系统设置模块化：存储路径 / 队列与磁盘 / 日志管理 / 配置历史 / 生成模式与引擎 / 关于系统',
      '任务提交改为分镜粒度：单个生成 / 批量生成；工作台支持一键停止任务',
      '参考图选择去分类化：首帧 / 尾帧 / 参考图各自独立管理',
      '单分镜任务不再产生重复成片文件；端口占用启动预检与友好提示',
    ],
  }],
  en: [{
    v: '0.0.1', date: '2026-09-27', items: [
      'System settings reorganized into modules; new "About" page',
      'Per-shot submission (single / batch generate) and one-click task stop',
      'Reference image picking de-categorized: frames and refs independent',
      'No duplicate merged file for single-shot tasks; port-in-use precheck',
    ],
  }],
};

// 功能模块注册表：id / 标题 / 图标 / 描述 / 表单 / 应用（无 apply = 只读操作）
const MODULES = [
  {
    id: 'storage', title: '存储路径', icon: 'folder',
    desc: '素材库本地存储位置',
    body: d => `
      <div class="note" style="margin-bottom:6px">
        所有素材与生成内容均保存在本地文件系统；素材库按图片 / 视频 /
        音频三个独立分类管理，上传时自动归入对应分类。留空使用默认位置
        （data/storage/ 下）。修改后立即生效，已有文件不会迁移。</div>
      ${LIBS.map(([b, title, desc]) => `
        <label>${title}</label>
        <input id="setPath-${b}" value="${esc(d.custom[b] || '')}"
          placeholder="${esc((d.buckets[b] || {}).path || '默认位置')}">
        <div class="note" style="margin:2px 0 8px">${desc}</div>`).join('')}`,
    apply: async d => {
      for (const [b] of LIBS) {
        if (!SETTABLE.has(b))              // 保存前校验库名，避免无效请求
          throw new Error('不支持的存储库: ' + b);
        await API.post('/api/storage', {
          bucket: b, path: el('setPath-' + b).value.trim(),
        });
      }
      await Settings._refresh(['st']);
      // 路径即时生效：重载素材库清单并刷新分镜面板，后续选择素材即为新库内容
      await Media.load();
      window.Segments.renderAll();
    },
  },
  {
    id: 'queue', title: '队列与磁盘', icon: 'stack',
    desc: '任务上限与磁盘预留空间）',
    body: d => `
      <div class="note" style="margin-bottom:6px">
        队列上限：排队 + 运行中的任务总数上限（单卡显存下一次只跑一个
        prompt，超限拒绝入队；0 = 不限）。磁盘预留：results / 素材 /
        引擎输入所在盘的最低剩余空间，不足拒绝入队（0 = 不检）。</div>
      <label>队列上限（个）</label>
      <input id="setQueueLimit" type="number" min="0" value="${d.s.queue_limit ?? 3}">
      <label>磁盘预留（GB）</label>
      <input id="setDiskReserve" type="number" min="0" step="0.5"
        value="${d.s.disk_reserve_gb ?? 5}">`,
    apply: async () => {
      await API.post('/api/settings', {
        queue_limit: Math.max(0, parseInt(el('setQueueLimit').value, 10) || 0),
        disk_reserve_gb: Math.max(0, parseFloat(el('setDiskReserve').value) || 0),
      });
      await Settings._refresh(['sd']);
      window.pollStatus && window.pollStatus();
    },
  },
  {
    id: 'log', title: '日志管理', icon: 'text',
    desc: '日志文件查看与清理',
    body: d => `
      <div class="note" style="margin-bottom:6px">
        当前日志固定写入 <code>xqwui.log</code>；达到单文件上限或跨过时间周期
        时自动切割为 <code>xqwui-日期-时间.log</code>，按份数与总占用两个上限
        保留（超限从最旧开始删除），开启归档后由后台线程压缩为
        <code>.log.gz</code>。切割在毫秒级完成，期间其它线程的日志会短暂排队
        后写入新文件，不丢日志。</div>
      <label>单文件上限（MB，0 = 不按大小切割）</label>
      <input id="setLogMax" type="number" min="0" step="1"
        value="${d.lc.max_mb ?? 20}">
      <label>时间切割周期（与大小任一先到即切）</label>
      <select id="setLogWhen">
        ${LOG_WHEN.map(([v, t]) => `<option value="${v}"
          ${(d.lc.when || 'midnight') === v ? 'selected' : ''}>${t}</option>`).join('')}
      </select>
      <label>保留份数（不含当前文件，0 = 不保留历史）</label>
      <input id="setLogKeep" type="number" min="0" step="1"
        value="${d.lc.keep_files ?? 10}">
      <label>目录总占用上限（MB，0 = 不限）</label>
      <input id="setLogTotal" type="number" min="0" step="10"
        value="${d.lc.max_total_mb ?? 200}">
      <label style="display:flex;align-items:center;gap:6px">
        <input type="checkbox" id="setLogGzip" ${d.lc.gzip ? 'checked' : ''}>
        切割后 gzip 归档历史日志
      </label>
      <div class="psec-h">日志文件</div>
      ${Settings._logHtml(d.lg)}`,
    apply: async () => {
      await API.post('/api/settings', {
        log_rotate_max_mb: Math.max(0, parseFloat(el('setLogMax').value) || 0),
        log_rotate_when: el('setLogWhen').value,
        log_keep_files: Math.max(0, parseInt(el('setLogKeep').value, 10) || 0),
        log_max_total_mb: Math.max(0, parseFloat(el('setLogTotal').value) || 0),
        log_archive_gzip: el('setLogGzip').checked ? 1 : 0,
      });
      await Settings._refresh(['sd', 'lg']);
    },
  },
  {
    id: 'snap', title: '配置历史', icon: 'retry',
    desc: '引擎参数回滚',
    body: d => `
      <div class="note" style="margin-bottom:6px">
        快照在「生成模式 → 保存」时自动生成；回滚会先用当前配置存一份快照，
        再整体覆盖（含引擎地址 / 队列 / 磁盘 / 日志策略）。</div>
      ${Settings._snapHtml(d.snap)}`,
    apply: null,                        // 只读列表 + 行内回滚操作
  },
  {
    id: 'engine', title: '生成模式与引擎', icon: 'bolt',
    desc: '本地 / 云端引擎参数',
    delegate: true,                     // 转交 GenMode 弹窗（自带保存 / 测试连接）
  },
  {
    id: 'about', title: '关于系统', icon: 'gear',
    desc: '软件信息 / 版本 / 许可 / 更新',
    body: () => Settings._aboutBody(),
    apply: null,                        // 展示型模块：检查更新 / 复制版本信息
  },
];

export const Settings = {
  _d: null,                            // 配置数据缓存（open 时填充）
  _cur: null,                          // 当前打开的模块 id（null = 总览）

  /** 入口：拉取全部配置 → 模块卡片总览 */
  async open() {
    const [sd, st, snap, lg, ab] = await Promise.all([
      API.soft(API.get('/api/settings'), '设置'),
      API.soft(API.get('/api/storage'), '存储配置'),
      API.soft(API.get('/api/settings/snapshots'), '配置历史'),
      API.soft(API.get('/api/logs'), '日志'),
      API.soft(API.get('/api/system/about'), '关于系统'),
    ]);
    if (!sd) return;
    const lc = (lg && lg.config) || {};
    Settings._d = {
      s: sd.settings || {},
      custom: st ? (st.custom || {}) : {},
      buckets: st ? (st.buckets || {}) : {},
      snap, lg, lc, about: ab || {},
    };
    Settings._cur = null;
    Settings._hub();
  },

  /** 模块总览：分类卡片 */
  _hub() {
    Settings._cur = null;
    UI.showModal('modalCfg', `
      <h3>${icon('gear')}系统设置
        <span class="note">按功能模块管理 · 点击卡片进入对应配置</span></h3>
      <div class="setgrid">${MODULES.map(m => `
        <button class="setcard" onclick="Settings.openModule('${m.id}')">
          ${icon(m.icon, 18)}<b>${m.title}</b>
          <span class="note">${m.desc}</span>
        </button>`).join('')}</div>
      <div class="mfoot" style="margin-top:8px;padding-top:8px">
        <button class="mini" onclick="UI.showStatus()">${icon('chart', 12)}系统状态详情</button>
        <span class="note" style="margin-left:auto">引擎连接状态也可点击顶部「引擎在线 / 离线」徽标查看</span>
      </div>`);
  },

  /** 打开单个功能模块的专属配置弹窗（复用 modalCfg，内容整帧重绘） */
  openModule(id) {
    const m = MODULES.find(x => x.id === id);
    if (!m) return;
    if (m.delegate) {                  // 生成模式与引擎：GenMode 自有弹窗
      window.GenMode && window.GenMode.open(window.GenMode.current());
      return;
    }
    const d = Settings._d || {};
    Settings._cur = id;
    UI.showModal('modalCfg', `
      <h3>${icon(m.icon)}${m.title}<span class="note">${m.desc}</span></h3>
      ${m.body(d)}
      <div class="mfoot" style="margin-top:8px;padding-top:8px">
        <button class="mini" onclick="Settings._hub()">${icon('stack', 12)}返回总览</button>
        <span class="spacer"></span>
        <button class="mini" onclick="UI.closeModal('modalCfg')">取消</button>
        ${m.apply ? `<button class="mini primary" onclick="Settings.applyModule()">
          ${icon('check', 12)}应用</button>` : ''}
      </div>`);
  },

  /** 应用当前模块：仅提交该模块字段；成功后刷新缓存并重绘本模块 */
  async applyModule() {
    const m = MODULES.find(x => x.id === Settings._cur);
    if (!m || !m.apply) return;
    try {
      await m.apply(Settings._d);
      toast(`${m.title}已应用`, 'ok');
      Settings.openModule(m.id);         // 重绘：回填生效后的值 / 刷新清单
    } catch (e) { toast(`应用失败：${e.message}`, 'err'); }
  },

  /** 刷新缓存数据（keys ⊆ sd/st/lg/snap） */
  async _refresh(keys) {
    const d = Settings._d || (Settings._d = {});
    for (const k of keys) {
      if (k === 'sd') {
        const sd = await API.soft(API.get('/api/settings'), '设置');
        if (sd) d.s = sd.settings || {};
      } else if (k === 'st') {
        const st = await API.soft(API.get('/api/storage'), '存储配置');
        if (st) { d.custom = st.custom || {}; d.buckets = st.buckets || {}; }
      } else if (k === 'lg') {
        d.lg = await API.soft(API.get('/api/logs'), '日志');
        d.lc = (d.lg && d.lg.config) || d.lc || {};
      } else if (k === 'snap') {
        d.snap = await API.soft(API.get('/api/settings/snapshots'), '配置历史');
      }
    }
  },

  /** 重绘当前界面：模块打开中重绘该模块，否则回总览 */
  _repaint() {
    Settings._cur ? Settings.openModule(Settings._cur) : Settings._hub();
  },

  // ------------------------------------------------------------ 日志模块
  /** 日志文件清单：名称 / 体积 / 时间 + 查看 / 切割 / 清理 */
  _logHtml(lg) {
    const items = (lg && lg.files) || [];
    if (!items.length) return '<div class="note">暂无日志文件</div>';
    return `${items.map(f => `
      <div class="prow">
        <span class="pname">${esc(f.file)}</span>
        <span class="meta">${fmtBytes(f.size)}</span>
        <span class="meta">${fmtTime(f.mtime_ms)}</span>
        ${f.current ? '<span class="meta">当前</span>' : `
          <button class="mini" onclick="Settings.viewLog('${esc(f.file)}')"
            title="查看末尾 300 行">查看</button>`}
      </div>`).join('')}
      <div class="note" style="margin:4px 0 6px">共 ${items.length} 个文件 ·
        占用 ${fmtBytes(lg.total || 0)}</div>
      <div class="mfoot" style="margin-top:6px;padding-top:6px">
        <button class="mini" onclick="Settings.rotateLog()">${icon('refresh', 12)}立即切割</button>
        <button class="mini" onclick="Settings.pruneLog()">${icon('trash', 12)}按策略清理</button>
      </div>`;
  },

  /** 查看单个日志（末尾 300 行，支持 .gz） */
  async viewLog(file) {
    const r = await API.soft(API.get(
      `/api/logs/content?file=${encodeURIComponent(file)}&lines=300`), '日志');
    if (!r) return;
    UI.showModal('modalStat', `
      <h3>${icon('frames', 15)}日志 ${esc(r.file)}</h3>
      <div class="note" style="margin-bottom:6px">${fmtBytes(r.size)} ·
        共 ${r.total_lines} 行${r.truncated ? `（仅显示末尾 ${r.shown} 行）` : ''}</div>
      <pre style="max-height:56vh;overflow:auto;font-size:11px;line-height:1.55;
        white-space:pre-wrap;word-break:break-all">${esc((r.lines || []).join('\n'))}</pre>`);
  },

  async rotateLog() {
    try {
      const r = await API.post('/api/logs/rotate', {});
      toast(r.file ? '已切割：' + r.file : '当前日志为空，未切割', 'ok');
      await Settings._refresh(['lg']);
      Settings._repaint();
    } catch (e) { toast('切割失败：' + e.message, 'err'); }
  },

  async pruneLog() {
    try {
      const r = await API.post('/api/logs/prune', {});
      toast(`已清理 ${r.removed.length} 个文件，释放 ${fmtBytes(r.freed)}`, 'ok');
      await Settings._refresh(['lg']);
      Settings._repaint();
    } catch (e) { toast('清理失败：' + e.message, 'err'); }
  },

  // ------------------------------------------------------------ 配置历史
  _snapHtml(snap) {
    const items = (snap && snap.snapshots) || [];
    if (!items.length)
      return '<div class="note">暂无快照 —— 在右侧悬浮按钮保存引擎参数后自动生成</div>';
    return items.map(x => `
      <div class="prow">
        <span class="pname">${fmtTime(x.saved_ms)}</span>
        <span class="meta">${esc(x.engine_url || '–')}</span>
        <span class="meta">队列 ${x.queue_limit ?? '–'} · 磁盘 ${x.disk_reserve_gb ?? '–'}GB</span>
        <button class="mini danger" onclick="Settings.rollback('${esc(x.file)}')"
          title="用该快照覆盖当前配置">回滚</button>
      </div>`).join('');
  },

  /** 回滚到指定配置快照（先自动快照当前配置，再整体覆盖） */
  async rollback(file) {
    if (!confirm('用该快照覆盖当前配置？（当前配置会先自动存为快照）')) return;
    try {
      await API.post('/api/settings/snapshots/rollback', { file });
      toast('已回滚配置', 'ok');
      await Settings._refresh(['sd', 'snap']);
      window.pollStatus && window.pollStatus();
      Settings._repaint();
    } catch (e) { toast('回滚失败：' + e.message, 'err'); }
  },

  // ------------------------------------------------------------ 关于系统
  /** 关于页正文：软件身份 / 版本 / 许可 / 支持 + 更新摘要与操作 */
  _aboutBody() {
    const a = (Settings._d && Settings._d.about) || {};
    const u = a.update;
    const cl = (CHANGELOG[loc()] || CHANGELOG.zh)[0] || {};
    return `
      <div class="abouthead">
        ${icon('gear', 26)}
        <div class="abt-id">
          <b>${esc(a.name || 'XQWUI 漫剧工作台')}</b>
          <span class="note">${esc(a.subtitle || '')}</span>
        </div>
        <span class="spacer"></span>
        <select id="aboutLocale" style="width:auto" title="Language"
          onchange="Settings.setLocale(this.value)">
          <option value="zh" ${loc() === 'zh' ? 'selected' : ''}>中文</option>
          <option value="en" ${loc() === 'en' ? 'selected' : ''}>English</option>
        </select>
      </div>
      <div class="env">
        <div class="kv"><b>${t('version')}</b><span>v${esc(a.version || '–')}
          · ${t('build')} ${esc(a.build_date || '–')}</span></div>
        <div class="kv"><b>${t('python')}</b><span>Python ${esc(a.python || '–')}</span></div>
        <div class="kv"><b>${t('dev')}</b><span>${esc(a.copyright || '')}</span></div>
        <div class="kv"><b>${t('license')}</b><span>
          <a href="${esc(a.license_url || '#')}" target="_blank" rel="noopener"
            style="color:var(--acc-h)">${esc(a.license || '')}</a></span></div>
        <div class="kv"><b>${t('support')}</b><span>${a.homepage
          ? `<a href="${esc(a.homepage)}" target="_blank" rel="noopener"
               style="color:var(--acc-h)">${esc(a.homepage)}</a>`
          : t('noHome')}</span></div>
      </div>
      <div class="psec-h">${t('changelog')}</div>
      <div class="note" style="margin-bottom:4px">v${esc(cl.v || '')} · ${esc(cl.date || '')}</div>
      <ul class="abt-ul">${(cl.items || []).map(x => `<li>${esc(x)}</li>`).join('')}</ul>
      ${u ? `<div class="note" style="margin:8px 0">${
        !u.ok ? `${t('checkFail')}：${esc(u.reason || '')}`
        : u.up_to_date ? `✓ ${t('upToDate')}（v${esc(a.version || '')}）`
        : `${t('newVer')}：v${esc(u.latest)}${
            u.notes ? ' — ' + esc(u.notes).slice(0, 200) : ''}`}</div>` : ''}
      <div class="mfoot" style="margin-top:8px;padding-top:8px">
        <button class="mini" id="aboutCheck" onclick="Settings.checkUpdate()">
          ${icon('refresh', 12)}${t('check')}</button>
        <button class="mini" onclick="Settings.copyAbout()">
          ${icon('save', 12)}${t('copy')}</button>
        <span class="note" style="margin-left:auto">${t('copyHint')}</span>
      </div>`;
  },

  /** 切换关于页语言（持久化后以新语言重绘当前模块） */
  setLocale(v) {
    try { localStorage.setItem('xqwui.locale', v); } catch { /* 隐私模式忽略 */ }
    Settings.openModule('about');
  },

  /** 在线检查更新：走后端 /api/system/about?check=1（更新源未配置时本地提示） */
  async checkUpdate() {
    const btn = el('aboutCheck');
    if (btn) { btn.disabled = true; btn.textContent = t('checking'); }
    try {
      const r = await API.soft(API.get('/api/system/about?check=1'), '检查更新');
      if (!r) return;
      Settings._d.about = r;
      Settings.openModule('about');      // 重绘展示检查结果
      const u = r.update || {};
      if (!u.ok) toast(`${t('checkFail')}：${u.reason || ''}`, 'err');
      else if (u.up_to_date) toast(t('upToDate'), 'ok');
      else toast(`${t('newVer')}：v${u.latest}`, 'ok');
    } catch (e) { toast(`${t('checkFail')}：${e.message}`, 'err'); }
  },

  /** 复制版本信息（剪贴板 API 不可用时退回 execCommand） */
  async copyAbout() {
    const a = (Settings._d && Settings._d.about) || {};
    const info = JSON.stringify({
      name: a.name, version: a.version, build_date: a.build_date,
      python: a.python, license: a.license, data_dir: a.data_dir,
      ua: navigator.userAgent,
    }, null, 2);
    try {
      await navigator.clipboard.writeText(info);
      toast(t('copied'), 'ok');
    } catch {
      const ta = document.createElement('textarea');
      ta.value = info;
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand('copy');
        toast(t('copied'), 'ok');
      } catch { toast(t('copyFail'), 'err'); }
      ta.remove();
    }
  },
};
