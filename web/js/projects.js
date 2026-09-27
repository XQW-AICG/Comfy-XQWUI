// projects.js —— 项目管理首页：项目列表 / 创建 / 删除 / 进入工作台
// （window.Projects；首页视图）
//
// 数据源：/api/projects（注册表）+ /api/autosave?project=<id>（每项目
// 独立自动存档）。切换 / 删除项目时工作台数据互不串扰：切换前先把当前
// 项目草稿落盘，再加载目标项目的存档（无存档则进入空白工作台）。

import { el, esc, icon, toast, fmtTime } from './core.js';
import { API } from './api.js';

export const Projects = {
  data: { projects: [], current: '' },
  firstRun: false,

  currentId() { return Projects.data.current || ''; },
  currentName() {
    const id = Projects.currentId();
    return (Projects.data.projects.find(p => p.id === id) || {}).name || '';
  },

  /** 启动装配：注册表就绪（无项目自动建默认项目 + 迁移旧存档） */
  async ensure() {
    const d = await API.soft(API.get('/api/projects'), '项目');
    if (d) {
      Projects.data = { projects: d.projects || [], current: d.current || '' };
      Projects.firstRun = !!d.first_run;
    }
    Projects.renderChip();
  },

  renderChip() {
    const n = el('projName');
    if (n) n.textContent = Projects.currentName()
      ? '当前项目：' + Projects.currentName() : '';
  },

  /** 进入首页并渲染项目列表 */
  openHome() {
    const btn = document.querySelector('.nav button[data-v="home"]');
    window.UI.nav('home', btn);   // nav 钩子内会调用 Projects.render()
  },

  /** 创建新项目：弹窗填写名称 / 描述（宫格首格入口） */
  openCreate() {
    window.UI.showModal('modalProj', `
      <h3>${icon('plus')}新建项目</h3>
      <label>项目名称（必填）</label>
      <input id="projNameInput" maxlength="40" placeholder="例如：都市夜行 第一季">
      <label>项目描述（可选）</label>
      <textarea id="projDescInput" rows="3" maxlength="120"
        placeholder="一句话描述这个项目，会显示在项目卡片上…"></textarea>
      <div class="mfoot">
        <span class="note" style="margin-right:auto">创建后自动进入该项目的工作台</span>
        <button onclick="UI.closeModal('modalProj')">取消</button>
        <button class="primary" onclick="Projects.submitCreate()">${icon('check')}创建并进入</button>
      </div>`);
    const name = el('projNameInput');
    name.focus();
    name.addEventListener('keydown', e => {
      if (e.key === 'Enter') Projects.submitCreate();
    });
  },

  /** 提交创建：校验 → 入库 → 关弹窗 → 进入新项目工作台 */
  async submitCreate() {
    const name = el('projNameInput').value.trim();
    const desc = el('projDescInput').value.trim();
    if (!name) return toast('请填写项目名称', 'err');
    try {
      await window.Workbench._autosaveRaw();     // 当前项目草稿先落盘
      const r = await API.post('/api/projects',
        { name, desc });
      if (!r || !r.ok) throw new Error((r && r.error) || '创建失败');
      // 同步本地注册表（后端已把 current 指向新项目），
      // 否则 open() 在滞后的本地列表里找不到目标项目会静默返回
      if (!Projects.data.projects.some(p => p.id === r.project.id))
        Projects.data.projects.push(r.project);
      Projects.data.current = r.project.id;
      window.UI.closeModal('modalProj');
      toast('项目「' + r.project.name + '」已创建', 'ok');
      await Projects.open(r.project.id, { skipSelect: true });
    } catch (e) { toast('创建失败：' + e.message, 'err'); }
  },

  /** 删除项目（联动清理其自动存档；删空后首页仅显示新建入口） */
  async remove(id) {
    const p = Projects.data.projects.find(x => x.id === id);
    if (!p) return;
    if (!confirm('删除项目「' + p.name + '」？该项目的分镜草稿将一并删除（已提交的任务不受影响）。')) return;
    try {
      const r = await API.del('/api/projects/' + encodeURIComponent(id));
      if (!r || !r.ok) throw new Error((r && r.error) || '删除失败');
      toast('项目「' + p.name + '」已删除', 'ok');
      const d = await API.get('/api/projects');
      Projects.data = { projects: d.projects || [], current: d.current || '' };
      Projects.render();
      if (Projects.currentId()) {
        await window.Workbench.loadProject(Projects.currentId());
      }
      Projects.renderChip();
    } catch (e) { toast('删除失败：' + e.message, 'err'); }
  },

  /** 选择项目：落盘当前草稿 → 切换 → 加载目标项目数据 → 进入工作台 */
  async open(id, opts = {}) {
    const target = Projects.data.projects.find(p => p.id === id);
    if (!target) {
      // 本地列表滞后（如刚创建）：拉一次注册表重试，避免静默失败
      const d = await API.get('/api/projects');
      Projects.data = { projects: d.projects || [], current: d.current || '' };
      if (!Projects.data.projects.some(p => p.id === id)) {
        toast('项目不存在或已被删除', 'err');
        return;
      }
    }
    try {
      if (!opts.skipSelect) {
        await window.Workbench._autosaveRaw();
        await API.post('/api/projects/' + encodeURIComponent(id) + '/select', {});
      }
      Projects.data.current = id;
      await window.Workbench.loadProject(id);
      window.UI.nav('work');
      Projects.renderChip();
    } catch (e) { toast('进入项目失败：' + e.message, 'err'); }
  },

  /** 渲染项目宫格（新建首格 + 项目卡片，按创建时间排序） */
  render() {
    const list = Projects.data.projects || [];
    const cur = Projects.currentId();
    // 宫格首格：「新建项目」虚线入口（与其余宫格同一网格、同一尺寸体系）
    const newTile = '<div class="pcard pnew" onclick="Projects.openCreate()" ' +
      'tabindex="0" onkeydown="if(event.key===\'Enter\')Projects.openCreate()" ' +
      'title="新建项目">' +
      '<div class="pcover"><span class="pplus">' + icon('plus', 22) + '</span></div>' +
      '<div class="pbody"><div class="pname">新建项目</div>' +
      '<div class="pcmeta">填写名称与描述，创建独立的分镜项目</div></div></div>';
    // 排序：最新创建的在前（当前项目照常高亮，不强制置顶）
    const sorted = [...list].sort((a, b) =>
      (b.created_ms || 0) - (a.created_ms || 0));
    const cards = sorted.map(p => {
      const isCur = p.id === cur;
      const logo = esc((p.name || '项').trim().charAt(0).toUpperCase());
      const curTag = isCur ? '<span class="pcur">当前</span>' : '';
      const btnOpen = "<button class='mini primary' onclick='event.stopPropagation();Projects.open(\"" + p.id + "\")'>" + icon('play', 12) + '进入工作台</button>';
      const btnDel = "<button class='mini danger' onclick='event.stopPropagation();Projects.remove(\"" + p.id + "\")'>" + icon('trash', 12) + '删除</button>';
      const descLine = p.desc
        ? '<div class="pdesc" title="' + esc(p.desc) + '">' + esc(p.desc) + '</div>'
        : '';
      return '<div class="pcard' + (isCur ? ' cur' : '') + '" onclick="Projects.open(\'' + p.id + '\')" tabindex="0" ' +
        'onkeydown="if(event.key===\'Enter\')Projects.open(\'' + p.id + '\')">' +
        '<div class="pcover"><span class="plogo">' + logo + '</span>' + curTag + '</div>' +
        '<div class="pbody">' +
        '<div class="pname" title="' + esc(p.name) + '">' + esc(p.name) + '</div>' +
        descLine +
        '<div class="pcmeta">创建于 ' + fmtTime(p.created_ms) + '</div>' +
        '<div class="pcacts">' + btnOpen + btnDel + '</div>' +
        '</div></div>';
    });
    el('projList').innerHTML = newTile + cards.join('');
  },
};

// NOTE: 需要导入 toast —— 在 init.js 中由 core.js 的全局可见性保证；
// 此处显式补充导入以避免遗漏。

