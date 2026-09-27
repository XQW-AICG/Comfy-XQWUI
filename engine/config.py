"""config —— 引擎运行时配置。

环境变量优先，其次 data/engine_config.json，最后默认值。
"""
import json
import os

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("ENGINE_DATA",
                          os.path.join(_HERE, "data", "engine"))
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

DEFAULTS = {
    "host": os.environ.get("ENGINE_HOST", "0.0.0.0"),
    # 默认 8189：避开本机可能仍在运行的 ComfyUI Desktop（8188）
    "port": int(os.environ.get("ENGINE_PORT", "8189")),
    # 模型库目录列表（扫描 diffusion_models / text_encoders / vae / loras /
    # latent_upscale_models 子目录）；默认含仓库 models/ 与同级 ComfyUI 模型库
    "model_dirs": [],
    # 计算后端名（auto = 第一个 available 的；null = 未配置）
    "compute_backend": os.environ.get("ENGINE_BACKEND", "auto"),
    # 历史条数上限
    "history_max": 256,
    "max_filename_len": 80,
}

_cfg_cache = None


def _file_cfg() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_cfg() -> dict:
    global _cfg_cache
    if _cfg_cache is None:
        cfg = dict(DEFAULTS)
        cfg.update(_file_cfg())
        if not cfg["model_dirs"]:
            cfg["model_dirs"] = [
                os.path.join(_HERE, "models"),
                "/media/waroot/shuju/AI/ComfyUI/models",
            ]
        _cfg_cache = cfg
    return _cfg_cache


def save_cfg(update: dict) -> dict:
    cur = _file_cfg()
    cur.update(update or {})
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    _cfg_cache = None
    return get_cfg()


# ---- 目录约定

def input_dir() -> str:
    return os.path.join(DATA_DIR, "input")


def output_dir() -> str:
    return os.path.join(DATA_DIR, "output")


def temp_dir() -> str:
    return os.path.join(DATA_DIR, "temp")


def ensure_dirs():
    for d in (DATA_DIR, input_dir(), output_dir(), temp_dir()):
        os.makedirs(d, exist_ok=True)


def model_subdir(kind: str) -> list:
    """所有模型库中 kind 子目录的绝对路径列表（存在的）。"""
    out = []
    for base in get_cfg()["model_dirs"]:
        p = os.path.join(base, kind)
        if os.path.isdir(p):
            out.append(p)
    return out
