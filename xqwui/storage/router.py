"""router —— 本地存储路由器（纯本地方案）。

所有数据、素材与生成内容均存储于本地文件系统：
  materials → 素材库，按类型拆分为 images / videos / audios 三个独立
              分类目录（未单独设置时共用 materials_dir 或默认位置）
  results   → results_dir 配置（默认 data/storage/results）
  cache     → data/storage/cache（内部缓存，固定默认位置）

统一经 router 拿到的都是 StoredObject；/media 服务直接读本地文件。
materials 桶的读取 / 列举 / 媒体解析自动聚合三个分类；写入按扩展名
路由到对应分类根目录，路径配置修改后立即生效（后端按根目录重建）。
"""
import os
import threading

from xqwui import config as cfg
from xqwui.config import MATERIAL_KINDS
from xqwui.storage.base import StoredObject, StorageError  # noqa: F401 再导出
from xqwui.storage.local import LocalStorage

BUCKETS = ("materials", "results", "cache")

# 分类归属扩展名表（与前端 core.js / assets.py 的类型约定一致）
_KIND_EXTS = {
    "images": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"},
    "videos": {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".ts"},
    "audios": {".wav", ".mp3", ".flac", ".ogg", ".aac", ".m4a", ".opus"},
}

_lock = threading.Lock()
_backends: dict = {}          # (bucket, kind) → (realpath, LocalStorage)

# 参考图规范副本的长边上限（与 h3_video.py 的 ref_max_size 默认值对齐）：
# 上传的长边超过该值的图片自动生成等比缩小的 norm 副本，引擎同步与
# /media 预览默认取 norm（小图），省去每次生成重复传输/解码原图。
NORM_LIMIT = 864


def norm_key(key: str) -> str:
    """超大参考图的规范副本 key：{stem}.norm{ext}。"""
    stem, ext = os.path.splitext(str(key))
    return "%s.norm%s" % (stem, ext)


def _is_norm(key: str) -> bool:
    """norm 副本自身（列举时隐藏、不重复预处理）。"""
    stem, ext = os.path.splitext(os.path.basename(str(key)).lower())
    return stem.endswith(".norm") and ext in _KIND_EXTS["images"]


def material_kind(key: str) -> str:
    """按扩展名归类素材 → images / videos / audios（未知类型归 images）。"""
    ext = os.path.splitext(str(key))[1].lower()
    for kind, exts in _KIND_EXTS.items():
        if ext in exts:
            return kind
    return "images"


def backend_for(bucket: str, kind: str = "") -> LocalStorage:
    """按桶取本地后端；素材库按分类解析根目录（跟随配置自动重建）。"""
    if bucket not in BUCKETS:
        raise StorageError("未知存储桶: %s" % bucket)
    if bucket == "materials":
        if kind not in MATERIAL_KINDS:
            kind = material_kind(kind)   # 传入 key 时按扩展名归类
        root = os.path.realpath(cfg.materials_root(kind))
    else:
        kind = ""
        root = os.path.realpath(cfg.bucket_root(bucket))
    ck = (bucket, kind)
    with _lock:
        hit = _backends.get(ck)
        if hit and hit[0] == root:
            return hit[1]
        be = LocalStorage(root=root, flat=True)
        _backends[ck] = (root, be)
        return be


def _kind_of(bucket: str, key: str) -> str:
    return material_kind(key) if bucket == "materials" else ""


def put(bucket: str, key: str, data: bytes,
        content_type: str = "") -> StoredObject:
    """写入；素材库按扩展名路由到对应分类目录。"""
    return backend_for(bucket, _kind_of(bucket, key)).put(
        bucket, key, data, content_type)


def put_file(bucket: str, key: str, src_path: str,
             content_type: str = "") -> StoredObject:
    return backend_for(bucket, _kind_of(bucket, key)).upload_from(
        bucket, key, src_path, content_type)


def _materials_getters(key: str):
    """素材库三分类后端（读取顺序 images → videos → audios）。"""
    return (backend_for("materials", k) for k in MATERIAL_KINDS)


def get(bucket: str, key: str) -> bytes:
    if bucket != "materials":
        return backend_for(bucket).get(bucket, key)
    last = None
    for be in _materials_getters(key):
        try:
            return be.get(bucket, key)
        except StorageError as e:
            last = e
    raise last or StorageError("对象不存在: %s/%s" % (bucket, key))


def path_of(bucket: str, key: str) -> str:
    """对象本地文件路径（流式读取用；materials 按分类解析实际所在目录）。

    对象不存在时返回其分类预期路径——调用方需自行处理缺失。

    素材图片优先返回 norm 副本（存在时）：引擎同步 / 媒体预览默认拿
    长边 ≤ NORM_LIMIT 的小图；删除/写入仍以原 key 为准。"""
    if bucket != "materials":
        return backend_for(bucket).path_of(bucket, key)
    for be in _materials_getters(key):
        if be.exists("materials", key):
            if material_kind(key) == "images":
                nk = norm_key(key)
                if nk != key and be.exists("materials", nk):
                    return be.path_of("materials", nk)
            return be.path_of("materials", key)
    return backend_for("materials", key).path_of("materials", key)


def delete(bucket: str, key: str) -> bool:
    if bucket != "materials":
        return backend_for(bucket).delete(bucket, key)
    for be in _materials_getters(key):
        if be.delete("materials", key):
            nk = norm_key(key)      # 顺带清理 norm 副本，避免孤儿文件
            if nk != key:
                be.delete("materials", nk)
            return True
    return False


def exists(bucket: str, key: str) -> bool:
    if bucket != "materials":
        return backend_for(bucket).exists(bucket, key)
    return any(be.exists(bucket, key) for be in _materials_getters(key))


def list_objects(bucket: str, prefix: str = "", limit: int = 0) -> list:
    """按创建时间倒序列出；素材库聚合三个分类目录。
    去重规则：
      · 每个分类后端只产出属于该分类扩展名的文件（分类路径嵌套时，
        父目录下的其他类别文件不重复列出）；
      · 跨分类按文件名（basename）去重，同一文件只保留首次出现的 key。"""
    if bucket != "materials":
        out = backend_for(bucket).list(bucket, prefix, 0)
        return out[:limit] if limit else out
    out, seen = [], set()
    for kind in MATERIAL_KINDS:
        exts = _KIND_EXTS[kind]
        be = backend_for("materials", kind)
        for o in be.list(bucket, prefix, 0):
            if os.path.splitext(o.key)[1].lower() not in exts:
                continue
            if _is_norm(o.key):       # norm 副本对使用者不可见
                continue
            base = os.path.basename(o.key).lower()
            if base in seen:
                continue
            seen.add(base)
            out.append(o)
    out.sort(key=lambda o: -(o.created_ms or 0))
    return out[:limit] if limit else out


def open_path(bucket: str, key: str) -> str | None:
    if bucket != "materials":
        return backend_for(bucket).open_path(bucket, key)
    for be in _materials_getters(key):
        p = be.open_path(bucket, key)
        if p:
            return p
    return None


def download_to(bucket: str, key: str, dst: str) -> str:
    """确保 key 的内容在本地 dst 可用（合并/抽帧前置步骤）。"""
    if bucket != "materials":
        return backend_for(bucket).download_to(bucket, key, dst)
    for be in _materials_getters(key):
        p = be.open_path(bucket, key)
        if p:
            return be.download_to(bucket, key, dst)
    raise StorageError("对象不存在: %s/%s" % (bucket, key))


def media_url(bucket: str, key: str) -> str:
    """/media 路由的对外地址（前端直接引用）。"""
    from urllib.parse import quote
    return "/media/%s/%s" % (bucket, quote(key))


def resolve_media(bucket: str, key: str):
    """媒体服务：返回 (local_path|None, redirect_url|None)。纯本地无重定向。

    素材图片存在 norm 副本时默认返回小图（缩略图 / 预览免传原图），
    原件仍保留在盘上由引擎同步按需读取；对象不存在返回 (None, None)。"""
    if bucket == "materials":
        base = open_path(bucket, key)
        if not base:
            return None, None
        if material_kind(key) == "images":
            nk = norm_key(key)
            if nk != key:
                p = open_path(bucket, nk)
                if p:
                    return p, None
        return base, None
    return open_path(bucket, key), None


def _health_of(root: str) -> dict:
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        pass
    ok = os.path.isdir(root) and os.access(root, os.W_OK)
    return {"backend": "local", "path": root, "ok": ok,
            "error": "" if ok else "目录不可写或不存在"}


def health() -> dict:
    out = {}
    for bucket in ("results", "cache"):
        out[bucket] = _health_of(os.path.realpath(cfg.bucket_root(bucket)))
    kinds_ok = []
    for kind in MATERIAL_KINDS:
        h = _health_of(os.path.realpath(cfg.materials_root(kind)))
        out["materials_%s" % kind] = h
        kinds_ok.append(h["ok"])
    out["materials"] = {
        "backend": "local",
        "path": os.path.realpath(cfg.bucket_root("materials")),
        "ok": all(kinds_ok),
        "error": "" if all(kinds_ok) else "存在不可用的素材分类目录"}
    return out


def preprocess_material(key: str) -> dict:
    """上传后处理：超大参考图生成长边对齐 NORM_LIMIT 的 norm 副本。

    仅图片且长边 > NORM_LIMIT 时执行（Pillow 等比缩放，LANCZOS）：
      · JPEG quality=92/optimize；PNG/WEBP/BMP 原格式保存；
      · GIF 动图跳过（缩放会丢帧，参考图场景极少用到）；
      · 副本写为 {stem}.norm{ext}，与原件同目录同格式。
    返回 {"norm": norm_key}；无需处理 / 处理失败返回 {}。
    失败由调用方决定是否阻断——上传路径选择仅记日志。"""
    if material_kind(key) != "images" or _is_norm(key):
        return {}
    src = open_path("materials", key)
    if not src or not os.path.isfile(src):
        return {}
    try:
        from PIL import Image
        with Image.open(src) as im:
            if getattr(im, "is_animated", False):
                return {}
            w, h = im.size
            long_edge = max(w, h)
            if long_edge <= NORM_LIMIT:
                return {}
            fmt = (im.format or "").upper()
            if fmt not in ("JPEG", "PNG", "WEBP", "BMP"):
                return {}
            im.load()
            im.thumbnail((NORM_LIMIT, NORM_LIMIT), Image.LANCZOS)
            nk = norm_key(key)
            dst = backend_for("materials", "images").path_of("materials", nk)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if fmt == "JPEG":
                im.save(dst, "JPEG", quality=92, optimize=True)
            else:
                im.save(dst, fmt)
            return {"norm": nk}
    except Exception:
        return {}
