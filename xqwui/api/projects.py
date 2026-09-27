"""projects —— 项目管理 API。

  GET    /api/projects             项目清单 + 当前项目
  POST   /api/projects             创建项目 {name}
  DELETE /api/projects/{id}        删除项目（联动清理其自动存档）
  POST   /api/projects/{id}/select 切换当前项目
"""
from urllib.parse import unquote

from xqwui import projects as lib


def register(app):
    @app.get("/api/projects")
    def list_projects(ctx, q, body):
        d = lib.list_projects()
        ctx.json({"projects": d["projects"], "current": d["current"],
                  "first_run": bool(d.get("first_run"))})

    @app.post("/api/projects")
    def create_project(ctx, q, body):
        name = str((body or {}).get("name") or "")
        if not name.strip():
            ctx.error("项目名称不能为空")
        desc = str((body or {}).get("desc") or "")
        r = lib.create(name, desc)
        ctx.json(r)

    @app.delete("/api/projects/*")
    def delete_project(ctx, q, body):
        pid = unquote(ctx.h.path.split("?")[0].rsplit("/", 1)[-1])
        r = lib.remove(pid)
        if not r.get("ok"):
            ctx.error(r.get("error") or "删除失败", 404)
        ctx.json(r)

    @app.post("/api/projects/*")
    def project_actions(ctx, q, body):
        parts = [unquote(x) for x in
                 ctx.h.path.split("?")[0].split("/") if x]
        if len(parts) != 4 or parts[3] != "select":
            ctx.error("路径格式：/api/projects/{id}/select", 404)
        r = lib.select(parts[2])
        if not r.get("ok"):
            ctx.error(r.get("error") or "切换失败", 404)
        ctx.json(r)
