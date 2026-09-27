"""tasks —— 任务 API：提交 / 查询 / 控制 / 工作流预览。

REST：
  GET    /api/tasks                     任务列表（摘要；archived=1 查已归档）
  POST   /api/tasks                     提交任务 {name, config}
  GET    /api/tasks/{id}                任务详情（含段与媒体地址）
  DELETE /api/tasks/{id}                删除任务
  POST   /api/tasks/{id}/pause          暂停
  POST   /api/tasks/{id}/resume         恢复
  POST   /api/tasks/{id}/cancel         取消
  POST   /api/tasks/{id}/retry          重试 {from_segment?}（段级续跑）
  POST   /api/tasks/{id}/archive        归档（列表隐藏，可恢复）
  POST   /api/tasks/{id}/restore        取消归档
  POST   /api/tasks/batch               批量提交 {config|items, count,
                                          name_prefix, seed_strategy}
  GET    /api/batches                   批处理报告（按 batch_id 聚合）
  GET    /api/batches/{id}              批次任务明细
  POST   /api/workflow/preview          工作流预览 {config, seg?}
"""
import random
import urllib.parse

from xqwui.scheduler import get_manager
from xqwui.workflow import h3_video

BATCH_MAX = 50


def _parts(ctx) -> list:
    """/api/tasks/{id}/{action} → ["api","tasks",id,action]。"""
    return [urllib.parse.unquote(x)
            for x in ctx.h.path.split("?")[0].split("/") if x]


def _media_url(key: str) -> str:
    return ("/media/results/%s" % urllib.parse.quote(key)) if key else ""


def _detail(t: dict) -> dict:
    """任务详情增强：段产物与成片补媒体地址。"""
    for seg in t.get("segments") or []:
        seg["url"] = _media_url(seg.get("file") or "")
    res = t.get("result") or {}
    if res.get("merged"):
        res["url"] = _media_url(res["merged"])
    return t


def _batch_items(body: dict) -> list:
    """批量条目展开：base config × count（种子策略）或显式 items。"""
    items = body.get("items")
    if isinstance(items, list) and items:
        return [it for it in items if isinstance(it, dict)][:BATCH_MAX]
    base = body.get("config") or {}
    try:
        count = int(body.get("count") or 0)
    except (TypeError, ValueError):
        return []
    count = max(1, min(count, BATCH_MAX))
    strategy = str(body.get("seed_strategy") or "random")
    seed0 = base.get("seed")
    try:
        seed0 = int(seed0)
    except (TypeError, ValueError):
        seed0 = -1
    if seed0 < 0 or seed0 > 2 ** 32 - 1:
        seed0 = random.randrange(2 ** 32)     # inc/keep 的基准种子
    out = []
    for i in range(count):
        cfg = dict(base)
        if strategy == "inc":
            cfg["seed"] = (seed0 + i) % (2 ** 32)
        elif strategy == "keep":
            cfg["seed"] = seed0
        else:                                 # random：-1 = 每段各自随机
            cfg["seed"] = -1
        out.append({"config": cfg})
    return out


def register(app):
    mgr = get_manager

    @app.get("/api/tasks")
    def list_tasks(ctx, q, body):
        if (q.get("archived") or "") == "1":
            ctx.json({"tasks": mgr().summaries_archived(), "archived": True})
            return
        ctx.json({"tasks": mgr().summaries(), "archived": False})

    @app.post("/api/tasks")
    def submit_task(ctx, q, body):
        if not isinstance(body, dict):
            ctx.error("请求体必须是 JSON 对象")
        r = mgr().submit(body.get("config") or {},
                         str(body.get("name") or ""))
        if not r.get("ok"):
            ctx.error(r.get("error") or "提交失败", 400)
        ctx.json(r, 200)

    @app.post("/api/tasks/batch")
    def submit_batch(ctx, q, body):
        if not isinstance(body, dict):
            ctx.error("请求体必须是 JSON 对象")
        items = _batch_items(body)
        if not items:
            ctx.error("批量条目为空：需要 {config, count} 或 {items: [...]}",
                      400)
            return
        r = mgr().submit_batch(items, str(body.get("name_prefix") or ""))
        if not r.get("ok"):
            ctx.error(r.get("error") or "批量提交失败", 400)
            return
        ctx.json(r, 200)

    @app.get("/api/tasks/*")
    def task_routes(ctx, q, body):
        p = _parts(ctx)
        tid = p[2] if len(p) > 2 else ""
        if not tid:
            ctx.error("缺少任务 id", 404)
        t = mgr().get(tid)
        if not t:
            ctx.error("任务不存在: %s" % tid, 404)
        if len(p) == 3:                       # 详情
            return ctx.json({"task": _detail(t)})
        ctx.error("未知子路径", 404)

    @app.post("/api/tasks/*")
    def task_actions(ctx, q, body):
        p = _parts(ctx)
        if len(p) != 4:
            ctx.error("路径格式：/api/tasks/{id}/{action}", 404)
        tid, action = p[2], p[3]
        if action == "retry":
            frm = (body or {}).get("from_segment")
            if frm is not None and not isinstance(frm, int):
                try:
                    frm = int(frm)
                except (TypeError, ValueError):
                    ctx.error("from_segment 必须是整数", 400)
                    return
            r = mgr().retry(tid, frm)
        elif action == "archive":
            r = mgr().archive(tid)
        elif action == "restore":
            r = mgr().restore(tid)
        else:
            fn = {"pause": mgr().pause, "resume": mgr().resume,
                  "cancel": mgr().cancel}.get(action)
            if fn is None:
                ctx.error("未知操作: %s" % action, 404)
            r = fn(tid)
        ctx.json(r, 200 if r.get("ok") else 409)

    @app.delete("/api/tasks/*")
    def task_delete(ctx, q, body):
        p = _parts(ctx)
        if len(p) != 3:
            ctx.error("路径格式：/api/tasks/{id}", 404)
        r = mgr().delete(p[2])
        ctx.json(r, 200 if r.get("ok") else 409)

    # ---------------------------------------------------------- 批处理报告
    @app.get("/api/batches")
    def list_batches(ctx, q, body):
        ctx.json({"batches": mgr().batches()})

    @app.get("/api/batches/*")
    def batch_detail(ctx, q, body):
        bid = urllib.parse.unquote(
            ctx.h.path.split("?")[0].rsplit("/", 1)[-1])
        d = mgr().batch_detail(bid)
        if not d:
            ctx.error("批次不存在: %s" % bid, 404)
            return
        ctx.json(d)

    @app.post("/api/workflow/preview")
    def workflow_preview(ctx, q, body):
        if not isinstance(body, dict) or not body.get("config"):
            ctx.error("缺少 config")
        cfg = body["config"]
        seg = q.get("seg")
        seg_i = int(seg) if (seg or "").lstrip("-").isdigit() else None
        try:
            pv = h3_video.preview(cfg, seg_i)
        except ValueError as e:
            ctx.error("工作流构建失败: %s" % e)
        ctx.json({"preview": pv})
