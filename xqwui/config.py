"""config —— 应用后端配置（环境变量 + data/config.json）。"""
import os
import time

from shared.util import atomic_write_json, read_json

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("XQWUI_DATA", os.path.join(_HERE, "data"))
WEB_DIR = os.path.join(_HERE, "web")
CONFIG_FILE = os.environ.get("XQWUI_CONFIG_FILE",
                             os.path.join(DATA_DIR, "config.json"))

DEFAULTS = {
    "host": os.environ.get("XQWUI_HOST", "0.0.0.0"),
    "port": int(os.environ.get("XQWUI_PORT", "8900")),
    "token": os.environ.get("XQWUI_TOKEN", ""),
    # 引擎地址（Comfy 后端 API 规范；引擎默认端口 8189，见 engine/config.py）
    "engine_url": os.environ.get("ENGINE_URL", "http://127.0.0.1:8189"),
    # 引擎不可达时任务重试间隔（秒）
    "engine_retry": 3,
    # 云端生成引擎（预留配置；尚未接入生成链路）
    "cloud_engine_url": "",
    "cloud_engine_token": "",
    # 本地存储路径（空 = 使用 data/storage/<bucket> 默认位置）
    # 素材库按类型拆分为三个独立库（图片 / 视频 / 音频）；
    # 分类路径未设置时回退到旧版整体 materials_dir，再退默认位置
    "materials_dir": "",
    "materials_images_dir": "",
    "materials_videos_dir": "",
    "materials_audios_dir": "",
    "results_dir": "",
    # 队列深度上限（queued/running/pausing 计数；0 = 不限制）。
    # 单卡 12GB 显存下引擎一次只跑一个 prompt，限制排队长度可避免
    # 批量提交把磁盘与调度资源占满
    "queue_limit": 3,
    # 入队预检：results / 素材 / 引擎输入所在磁盘的剩余空间下限
    # （GB；0 = 不检查）。低于该值拒绝入队并提示清理
    "disk_reserve_gb": 5.0,
    # ---- 日志切割 / 保留 / 归档（详见 xqwui/logutil.py 顶部方案说明）
    # 单文件体积上限（MB；0 = 不按大小切割）
    "log_rotate_max_mb": 20.0,
    # 时间切割策略：off 不切 / hourly 每小时整点 / midnight 每日 0 点
    "log_rotate_when": "midnight",
    # 历史文件保留份数（不含当前文件；0 = 不保留）
    "log_keep_files": 10,
    # logs 目录总占用上限（MB；0 = 不限）
    "log_max_total_mb": 200.0,
    # 切割后 gzip 归档（1 开 / 0 关）
    "log_archive_gzip": 1,
    # 新文件首行写切割标记（1 开 / 0 关）
    "log_rotate_marker": 1,
    # 日志级别（空 = 跟随 XQWUI_LOG_LEVEL，默认 INFO）
    "log_level": "",
    # ---- 关于系统：在线检查更新的版本清单端点（返回 {version, notes}）
    # 与官网 / 支持页（均可留空 = 不启用，不做外网请求）
    "update_url": os.environ.get("XQWUI_UPDATE_URL", ""),
    "homepage": os.environ.get("XQWUI_HOMEPAGE", ""),
}

_cache = None


def load() -> dict:
    global _cache
    if _cache is None:
        cfg = dict(DEFAULTS)
        saved = read_json(CONFIG_FILE, {}) or {}
        for k, v in saved.items():      # 仅接受已知键（旧 storage 段自动忽略）
            if k in DEFAULTS:
                cfg[k] = v
        _cache = cfg
    return _cache


def save(update: dict) -> dict:
    cfg = load()
    for k, v in (update or {}).items():
        if k in DEFAULTS:
            cfg[k] = v
    os.makedirs(DATA_DIR, exist_ok=True)
    atomic_write_json(CONFIG_FILE, cfg)
    _cache = None
    return load()


# ---- 目录约定

# 素材库三分类（各自独立路径，实时生效）
MATERIAL_KINDS = ("images", "videos", "audios")


def tasks_dir() -> str:
    return os.path.join(DATA_DIR, "tasks")


def log_dir() -> str:
    """日志目录（当前日志 xqwui.log 与切割出的历史文件同在此处）。"""
    return os.path.join(DATA_DIR, "logs")


def archive_dir() -> str:
    """已归档任务目录（默认列表不可见，可恢复）。"""
    return os.path.join(DATA_DIR, "tasks", "archive")


def snapshots_dir() -> str:
    """配置快照目录（滚动保留最近 N 份）。"""
    return os.path.join(DATA_DIR, "config_snapshots")


SNAPSHOT_KEEP = 20        # 配置快照滚动保留份数


def snapshot_config() -> str:
    """把当前配置存为快照（滚动清理），返回快照文件名。"""
    d = snapshots_dir()
    os.makedirs(d, exist_ok=True)
    fn = "config_%s.json" % time.strftime("%Y%m%d_%H%M%S")
    atomic_write_json(os.path.join(d, fn), {
        "saved_ms": int(time.time() * 1000), "config": dict(load())})
    olds = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    for f in olds[:-SNAPSHOT_KEEP]:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    return fn


def list_snapshots() -> list:
    """配置快照清单（新的在前）。"""
    d = snapshots_dir()
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d), reverse=True):
        if not fn.endswith(".json"):
            continue
        data = read_json(os.path.join(d, fn), {}) or {}
        cfg = data.get("config") or {}
        out.append({"file": fn, "saved_ms": data.get("saved_ms") or 0,
                    "engine_url": str(cfg.get("engine_url") or ""),
                    "queue_limit": cfg.get("queue_limit"),
                    "disk_reserve_gb": cfg.get("disk_reserve_gb")})
    return out


def rollback_snapshot(file: str) -> dict:
    """回滚到指定快照（合并默认键后整体保存）。找不到/损坏抛 OSError。"""
    safe = os.path.basename(str(file or ""))
    data = read_json(os.path.join(snapshots_dir(), safe), None)
    if not data or not isinstance(data.get("config"), dict):
        raise OSError("快照不存在或已损坏: %s" % safe)
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in data["config"].items() if k in DEFAULTS})
    return save(merged)


def presets_dir() -> str:
    return os.path.join(DATA_DIR, "presets")


def storage_dir() -> str:
    return os.path.join(DATA_DIR, "storage")


def materials_root(kind: str = "") -> str:
    """素材库根目录：kind ∈ images/videos/audios（空 = 整体兜底根）。

    解析优先级：分类路径 materials_<kind>_dir → 旧版整体 materials_dir
    → 默认 data/storage/materials。分类未设置时与整体素材库共用同一
    目录（旧数据无需迁移）。
    """
    if kind in MATERIAL_KINDS:
        custom = str(load().get("materials_%s_dir" % kind) or "").strip()
        if custom:
            return os.path.abspath(os.path.expanduser(custom))
    custom = str(load().get("materials_dir") or "").strip()
    if custom:
        return os.path.abspath(os.path.expanduser(custom))
    return os.path.join(storage_dir(), "materials")


def bucket_root(bucket: str) -> str:
    """桶根目录：results 支持自定义路径，cache 固定默认位置。"""
    if bucket == "results":
        custom = str(load().get("results_dir") or "").strip()
    else:
        custom = ""
    if custom:
        return os.path.abspath(os.path.expanduser(custom))
    return os.path.join(storage_dir(), bucket)


def engine_data_dir() -> str:
    """引擎输入目录（素材同步目标）。"""
    return os.path.join(DATA_DIR, "engine", "input")


def ensure_dirs():
    dirs = [DATA_DIR, tasks_dir(), archive_dir(), snapshots_dir(),
            presets_dir(), storage_dir(), log_dir(),
            bucket_root("results"), engine_data_dir()]
    dirs += [materials_root(k) for k in MATERIAL_KINDS]
    for d in dirs:
        os.makedirs(d, exist_ok=True)
