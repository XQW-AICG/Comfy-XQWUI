// init.js —— 前端入口：暴露全局命名空间、恢复现场、连接 WS、首屏渲染

import { el, renderIcons, toast } from './core.js';
import { API } from './api.js';
import { WS } from './ws.js';
import { UI, pollStatus } from './ui.js';
import { Timeline } from './timeline.js';
import { Media } from './media.js';
import { Segments } from './segments.js';
import { Workbench } from './workbench.js';
import { Tasks } from './tasks.js';
import { Projects } from './projects.js';
import { Results } from './results.js';
import { Settings } from './settings.js';
import { GenMode } from './floatpanel.js';

// ---- 经典脚本 onclick 使用的全局命名空间 ----
Object.assign(window, {
  XQ: window.XQ, UI, Timeline, Media, Segments, Workbench,
  Tasks, Projects, Results, Settings, GenMode, pollStatus,
});

async function boot() {
  renderIcons();
  await Projects.ensure();                 // 项目注册表 + 当前项目（含旧存档迁移）
  Workbench.bindInputs();
  Media.bindRefHighlight(el('gprompt'));   // 全局提示词 @素材 引用高亮
  Media.bindCaretPos(el('gprompt'));       // 全局提示词 光标位置 + 单击聚焦
  // 关闭 / 隐藏页面时立即落盘，防止防抖窗口内的修改丢失
  ['pagehide', 'beforeunload', 'visibilitychange'].forEach(ev =>
    window.addEventListener(ev, () => {
      if (ev === 'visibilitychange' && document.visibilityState !== 'hidden') return;
      Workbench.flushAutosave();
    }));
  Segments.renderAll();

  // 1) 引擎状态首查 + 定时轮询（徽标 / 显存）
  pollStatus();
  setInterval(pollStatus, 8000);

  // 2) WS：hello 快照 + 事件流
  WS.on('hello', d => {
    if (d.engine) UI.setEngine(d.engine);
    (d.tasks || []).forEach(t => Tasks.upsert(t));
    if (!window.XQ.state._helloed) {
      window.XQ.state._helloed = true;
      Tasks.reload();
    }
  });
  WS.on('engine_status', () => pollStatus());
  WS.on('task_updated', t => Tasks.upsert(t));
  WS.on('task_progress', p => Tasks.progress(p));
  WS.on('task_merging', t => Tasks.merging(t));
  WS.on('task_done', t => {
    Tasks.upsert(t); Results.reload();
    Timeline.loadDonePreview().then(() => Timeline.render());
  });
  WS.connect();

  // 3) 素材清单 + 模型清单 + 生成模式记忆（并行）
  await Promise.all([Media.load(), Workbench.loadModels(), GenMode.init()]);

  // 4) 恢复当前项目的自动存档；无存档：首跑载示例分镜，其余进空白工作台
  const restored = await Workbench.restoreAutosave();
  if (!restored) {
    if (Projects.firstRun) Segments.loadDemo();
    else Segments.blank();
  }
  // 刷新原位恢复：URL hash 优先 → 上次停留视图 → 兜底（首跑进项目页，老用户进工作台）
  UI.nav(UI.viewFromHash() || UI.lastView() ||
    (Projects.firstRun ? 'home' : 'work'));
  Projects.renderChip();

  // 4.5) 成片预览：有已完成的同分镜数任务时，时间轴切换为成片模式
  await Timeline.loadDonePreview();
  Timeline.render();

  // 5) 成品库首查（首屏在 work 视图，仍预热数据）
  Results.load('videos');

  const st = window.XQ.state.engine || {};
  toast(restored ? '已恢复上次工作现场'
    : '欢迎来到 XQWUI 工作台 —— 已载入示例分镜', 'ok');
}

document.addEventListener('DOMContentLoaded', boot);
