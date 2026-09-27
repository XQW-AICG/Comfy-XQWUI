"""loader —— 计算后端发现与选择。

发现：扫描 engine/compute/backends/*.py，优先取模块级 BACKEND 类；
未导出时自动扫描模块内定义的 ComputeBackend 子类。
选择：配置 compute_backend = "auto" 时取第一个 available() 的后端
      （null 恒可用、优先级最低）；指定名字则精确匹配。
"""
import importlib
import os
import threading

from engine.compute.base import ComputeBackend, ComputeBackendError
from engine.config import get_cfg

_BACKENDS_DIR = os.path.join(os.path.dirname(__file__), "backends")
_lock = threading.Lock()
_cache: dict = {}
_current = None


def _discover() -> dict:
    out = {}
    if not os.path.isdir(_BACKENDS_DIR):
        return out
    for fn in sorted(os.listdir(_BACKENDS_DIR)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        mod_name = "engine.compute.backends." + fn[:-3]
        try:
            mod = importlib.import_module(mod_name)
            cls = getattr(mod, "BACKEND", None)
            if not (isinstance(cls, type)
                    and issubclass(cls, ComputeBackend)):
                # 回退：扫描模块内定义的 ComputeBackend 子类
                cls = next((v for _, v in sorted(vars(mod).items())
                            if isinstance(v, type)
                            and issubclass(v, ComputeBackend)
                            and v.__module__ == mod.__name__), None)
            if cls and issubclass(cls, ComputeBackend):
                out[cls.name] = cls
        except Exception as e:      # 单个后端损坏不影响其他
            print("[engine] 计算后端 %s 加载失败: %s" % (fn, e))
    return out


def list_backends() -> dict:
    with _lock:
        if not _cache:
            _cache.update(_discover())
        return dict(_cache)


def get_backend() -> ComputeBackend:
    """返回当前后端实例（进程内单例）。"""
    global _current
    with _lock:
        if _current is not None:
            return _current
        found = _discover()
        want = get_cfg().get("compute_backend", "auto")
        cls = None
        if want and want != "auto":
            cls = found.get(want)
            if cls is None:
                raise ComputeBackendError("未找到计算后端: %s" % want)
        else:
            avail = [c for n, c in sorted(found.items())
                     if n != "null" and c.available()]
            cls = avail[0] if avail else found.get("null")
        if cls is None:
            raise ComputeBackendError("没有任何可用的计算后端")
        _current = cls()
        print("[engine] 计算后端: %s" % _current.name)
        return _current
