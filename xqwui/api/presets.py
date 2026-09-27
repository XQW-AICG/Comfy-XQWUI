"""presets —— 方案预设与自动存档。

  GET    /api/presets           预设清单
  POST   /api/presets           保存 {name, config}
  GET    /api/presets/{name}    读取单个预设
  DELETE /api/presets/{name}    删除预设
  GET    /api/autosave          读取自动存档（_autosave.json）
  POST   /api/autosave          写入自动存档 {config}
  POST   /api/presets/export    配方包导出（zip 下载，字段 file）
  POST   /api/presets/import    配方包导入（multipart zip，?overwrite=1）
"""
import io
import json
import os
import tempfile
import time
import zipfile

from shared.util import atomic_write_json, read_json, safe_name
from xqwui import config as appcfg
from xqwui import projects

_MANIFEST = "manifest.json"


def _path(name: str) -> str:
    return os.path.join(appcfg.presets_dir(), safe_name(name, 60) + ".json")


def _entry(fn: str) -> dict:
    d = read_json(os.path.join(appcfg.presets_dir(), fn), {}) or {}
    return {"name": fn[:-5], "updated_ms": d.get("updated_ms") or 0,
            "hasConfig": bool(d.get("config"))}


def _preset_files() -> list:
    """可导出的预设文件名（排除自动存档等下划线开头）。"""
    d = appcfg.presets_dir()
    if not os.path.isdir(d):
        return []
    return [f for f in sorted(os.listdir(d))
            if f.endswith(".json") and not f.startswith("_")]


def register(app):
    @app.get("/api/presets")
    def list_presets(ctx, q, body):
        d = appcfg.presets_dir()
        os.makedirs(d, exist_ok=True)
        items = [_entry(f) for f in sorted(os.listdir(d))
                 if f.endswith(".json") and not f.startswith("_")]
        items.sort(key=lambda x: -x["updated_ms"])
        ctx.json({"presets": items})

    @app.post("/api/presets")
    def save_preset(ctx, q, body):
        name = str((body or {}).get("name") or "").strip()
        config = (body or {}).get("config")
        if not name:
            ctx.error("缺少预设名称")
        if not isinstance(config, dict):
            ctx.error("缺少 config")
        atomic_write_json(_path(name), {
            "name": name, "config": config,
            "updated_ms": int(time.time() * 1000)})
        ctx.json({"ok": True, "name": safe_name(name, 60)})

    @app.get("/api/presets/*")
    def get_preset(ctx, q, body):
        name = ctx.h.path.split("?")[0].rsplit("/", 1)[-1]
        d = read_json(_path(unquote(name)), None)
        if not d:
            ctx.error("预设不存在: %s" % name, 404)
        ctx.json({"preset": d})

    @app.delete("/api/presets/*")
    def delete_preset(ctx, q, body):
        name = unquote(ctx.h.path.split("?")[0].rsplit("/", 1)[-1])
        p = _path(name)
        if not os.path.isfile(p):
            ctx.error("预设不存在: %s" % name, 404)
        ctx.json({"ok": os.remove(p) is None})

    @app.get("/api/autosave")
    def get_autosave(ctx, q, body):
        """读取自动存档：?project=<id> 按项目读取（多项目隔离）；
        缺省读取旧版全局存档（兼容旧前端）。"""
        pid = str((q.get("project") or "").strip())
        if pid:
            ctx.json({"autosave": projects.get_autosave(pid)})
            return
        p = os.path.join(appcfg.presets_dir(), "_autosave.json")
        ctx.json({"autosave": read_json(p, None)})

    @app.post("/api/autosave")
    def save_autosave(ctx, q, body):
        """写入自动存档：config（当前模式参数 + 分镜 + 全局提示词）
        + global（生成模式 / 双模式参数集 / 激活分镜）一体持久化。
        body.project 存在时按项目隔离存储（data/autosave/<pid>.json）；
        global 缺省为空字典，兼容旧版仅含 config 的存档文件。"""
        config = (body or {}).get("config")
        if not isinstance(config, dict):
            ctx.error("缺少 config")
        glob = (body or {}).get("global")
        if not isinstance(glob, dict):
            glob = {}
        pid = str((body or {}).get("project") or "").strip()
        if pid:
            projects.save_autosave(pid, config, glob)
            ctx.json({"ok": True})
            return
        p = os.path.join(appcfg.presets_dir(), "_autosave.json")
        atomic_write_json(p, {"config": config, "global": glob,
                              "updated_ms": int(time.time() * 1000)})
        ctx.json({"ok": True})

    # ---------------------------------------------------------- 配方包
    @app.post("/api/presets/export")
    def export_presets(ctx, q, body):
        """配方包导出：全部预设打包 zip（含 manifest）供下载/迁移。"""
        files = _preset_files()
        if not files:
            ctx.error("没有可导出的预设", 400)
            return
        d = appcfg.presets_dir()
        fd, tmp = tempfile.mkstemp(prefix="xqwui_presets_", suffix=".zip")
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(_MANIFEST, json.dumps({
                    "app": "xqwui", "type": "presets", "version": 1,
                    "count": len(files),
                    "exported_ms": int(time.time() * 1000)},
                    ensure_ascii=False, indent=1))
                for f in files:
                    zf.write(os.path.join(d, f), "presets/" + f)
            ctx.file(tmp, download="xqwui-presets.zip")
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    @app.post("/api/presets/import")
    def import_presets(ctx, q, body):
        """配方包导入：multipart zip（字段 file）；?overwrite=1 覆盖同名。"""
        part = (body or {}).get("file") or {}
        data = part.get("data") or b""
        if not data:
            ctx.error("缺少上传文件（multipart 字段 file）")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            ctx.error("不是有效的 zip 文件", 400)
            return
        overwrite = (q.get("overwrite") or "") == "1"
        imported, skipped, failed = [], [], []
        for n in zf.namelist():
            base = os.path.basename(n)
            if not n.endswith(".json") or base.startswith("_") \
                    or base == _MANIFEST or not base:
                continue
            try:
                d = json.loads(zf.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, KeyError):
                failed.append({"name": base, "error": "JSON 解析失败"})
                continue
            if not isinstance(d, dict) or not isinstance(d.get("config"),
                                                         dict):
                failed.append({"name": base, "error": "结构不合法（缺 config）"})
                continue
            name = safe_name(str(d.get("name") or base[:-5]), 60)
            p = _path(name)
            if os.path.isfile(p) and not overwrite:
                skipped.append(name)
                continue
            atomic_write_json(p, {"name": name, "config": d["config"],
                                  "updated_ms": int(time.time() * 1000)})
            imported.append(name)
        ctx.json({"ok": True, "imported": imported,
                  "skipped": skipped, "failed": failed})
