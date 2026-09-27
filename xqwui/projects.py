"""projects —— 项目管理：多项目的分镜数据隔离。

data/projects.json 注册表：{"projects":[{id,name,created_ms}], "current":id}
每个项目独立自动存档：data/autosave/<pid>.json（结构与旧 _autosave.json
完全一致，仅按项目分文件）。

不自动创建「默认项目」：无项目时首页仅显示「新建项目」入口；
创建第一个项目时，若存在旧版全局自动存档（presets_dir/_autosave.json）
则迁移到该项目名下（原文件改名保留）。
"""
import os
import time
import uuid

from shared.util import atomic_write_json, read_json

from xqwui import config as appcfg

REG_FILE = "projects.json"


def _reg_path():
    return os.path.join(appcfg.DATA_DIR, REG_FILE)


def _as_dir():
    return os.path.join(appcfg.DATA_DIR, "autosave")


def _as_path(pid: str) -> str:
    safe = "".join(c for c in pid if c.isalnum() or c in "_-")
    return os.path.join(_as_dir(), safe + ".json")


def _new_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def _read_reg() -> dict:
    d = read_json(_reg_path(), None) or {}
    if not isinstance(d.get("projects"), list) or not d["projects"]:
        d = {"projects": [], "current": ""}
    return d


def _default_name(taken: set) -> str:
    """空名创建时的兜底名（仅 API 直接调用，前端强制填写名称）。"""
    base, n = "未命名项目", 1
    while base in taken:
        n += 1
        base = "默认项目 %d" % n
    return base


def _make(name: str, desc: str = "") -> dict:
    return {"id": _new_id(), "name": name, "desc": (desc or "").strip()[:120],
            "created_ms": int(time.time() * 1000)}


def _write_reg(d: dict):
    atomic_write_json(_reg_path(), d)


def ensure() -> dict:
    """注册表就绪：不自动创建项目（无项目时 current 为空串）。

    校正失效的 current 指向（指向已删除项目时落到第一个项目）。"""
    d = _read_reg()
    if d.get("current") not in {p["id"] for p in d["projects"]}:
        d["current"] = d["projects"][0]["id"] if d["projects"] else ""
        _write_reg(d)
    return d


def _migrate_legacy_autosave(pid: str):
    """旧版全局自动存档 → 首个创建的项目名下（原文件改名保留）。"""
    legacy = os.path.join(appcfg.presets_dir(), "_autosave.json")
    if not os.path.isfile(legacy):
        return
    os.makedirs(_as_dir(), exist_ok=True)
    try:
        os.replace(legacy, _as_path(pid))
        from xqwui import logutil
        logutil.get("projects").info(
            "op=migrate 旧版全局自动存档已迁入项目=%s", pid)
    except OSError:
        pass


def list_projects() -> dict:
    return ensure()


def current_id() -> str:
    return ensure()["current"]


def current_name() -> str:
    d = ensure()
    return next((p["name"] for p in d["projects"]
                 if p["id"] == d["current"]), "")


def create(name: str, desc: str = "") -> dict:
    d = _read_reg()
    name = (name or "").strip() or _default_name(
        {p["name"] for p in d["projects"]})
    p = _make(name[:40], desc)
    first = not d["projects"]
    d["projects"].append(p)
    d["current"] = p["id"]                # 新建即进入该项目
    _write_reg(d)
    if first:
        _migrate_legacy_autosave(p["id"])  # 旧版全局存档 → 首个项目名下
    return {"ok": True, "project": dict(p)}


def remove(pid: str) -> dict:
    d = _read_reg()
    before = len(d["projects"])
    d["projects"] = [p for p in d["projects"] if p["id"] != pid]
    if len(d["projects"]) == before:
        return {"ok": False, "error": "项目不存在"}
    try:
        os.remove(_as_path(pid))          # 项目删除时同步清理其自动存档
    except OSError:
        pass
    # 删空后不自动补项目：无项目时首页仅显示「新建项目」入口
    if d.get("current") not in {p["id"] for p in d["projects"]}:
        d["current"] = d["projects"][0]["id"] if d["projects"] else ""
    _write_reg(d)
    return {"ok": True, "current": d["current"]}


def select(pid: str) -> dict:
    d = _read_reg()
    if pid not in {p["id"] for p in d["projects"]}:
        return {"ok": False, "error": "项目不存在"}
    d["current"] = pid
    _write_reg(d)
    return {"ok": True, "current": pid}


def get_autosave(pid: str):
    return read_json(_as_path(pid), None)


def save_autosave(pid: str, config: dict, glob: dict):
    os.makedirs(_as_dir(), exist_ok=True)
    atomic_write_json(_as_path(pid), {
        "config": config, "global": glob,
        "updated_ms": int(time.time() * 1000)})
