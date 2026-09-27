"""assets —— 素材与成品 API + /media 媒体服务。

  GET    /api/assets             素材清单（materials 桶）
  POST   /api/assets             上传素材（multipart: file）
  DELETE /api/assets/{key}       删除素材
  GET    /api/results            成品清单（results 桶）
  DELETE /api/results/{key}      删除成品
  GET    /media/{bucket}/{key}   媒体读取（本地文件直读，支持 Range）
"""
import logging
import os
from urllib.parse import quote, unquote

from xqwui.storage import router as storage

LOG = logging.getLogger("xqwui.api.assets")

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
        ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
        ".mov": "video/quicktime", ".avi": "video/x-msvideo",
        ".m4v": "video/x-m4v", ".ts": "video/mp2t",
        ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
        ".ogg": "audio/ogg", ".aac": "audio/aac", ".m4a": "audio/mp4",
        ".opus": "audio/opus", ".json": "application/json"}

EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
        ".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".ts",
        ".wav", ".mp3", ".flac", ".ogg", ".aac", ".m4a", ".opus")

# 上传校验：扩展名白名单（归类 + 大小上限）+ 文件头魔数嗅探。
# 魔数防「改扩展名」伪造（容器格式只校验特征偏移）；上限按类型区分，
# 视频上限放宽（素材视频常见几百 MB），图片/音频从严。
_KIND_EXTS = {
    "images": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"},
    "videos": {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".ts"},
    "audios": {".wav", ".mp3", ".flac", ".ogg", ".aac", ".m4a", ".opus"},
}
_MAX_BYTES = {"images": 30 * 1024 ** 2,
              "videos": 500 * 1024 ** 2,
              "audios": 50 * 1024 ** 2}


def _ext_kind(ext: str) -> str:
    for k, exts in _KIND_EXTS.items():
        if ext in exts:
            return k
    return ""


def _head_ok(ext: str, d: bytes) -> bool:
    h = d[:16]
    if ext == ".png":
        return h.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".jpg", ".jpeg"):
        return h.startswith(b"\xff\xd8\xff")
    if ext == ".gif":
        return h[:6] in (b"GIF87a", b"GIF89a")
    if ext == ".bmp":
        return h.startswith(b"BM")
    if ext == ".webp":
        return h[:4] == b"RIFF" and h[8:12] == b"WEBP"
    if ext in (".mp4", ".mov", ".m4v", ".m4a"):
        return len(d) >= 12 and h[4:8] == b"ftyp"
    if ext == ".ts":
        return d[:1] == b"\x47"
    if ext in (".mkv", ".webm"):
        return h.startswith(b"\x1a\x45\xdf\xa3")
    if ext == ".avi":
        return h[:4] == b"RIFF" and h[8:12] == b"AVI "
    if ext == ".wav":
        return h[:4] == b"RIFF" and h[8:12] == b"WAVE"
    if ext == ".mp3":
        return h.startswith(b"ID3") or h[:2] in (b"\xff\xfb", b"\xff\xf3",
                                                 b"\xff\xf2")
    if ext == ".flac":
        return h.startswith(b"fLaC")
    if ext in (".ogg", ".opus"):
        return h.startswith(b"OggS")
    if ext == ".aac":
        return h[:2] in (b"\xff\xf1", b"\xff\xf9")
    return False


def _ctype(key: str) -> str:
    return MIME.get(os.path.splitext(key)[1].lower(),
                    "application/octet-stream")


def _obj(o: storage.StoredObject) -> dict:
    return {"key": o.key, "size": o.size, "content_type": o.content_type,
            "created_ms": o.created_ms,
            "url": "/media/%s/%s" % (o.bucket, quote(o.key))}


def _list_bucket(ctx, bucket: str, q: dict):
    prefix = q.get("prefix") or ""
    limit = int(q.get("limit") or 0)
    try:
        objs = storage.list_objects(bucket, prefix, limit)
    except storage.StorageError as e:
        ctx.error("存储读取失败: %s" % e, 500)
    media = [x for x in objs if os.path.splitext(x.key)[1].lower() in EXTS]
    ctx.json({"bucket": bucket, "total": len(objs),
              "items": [_obj(o) for o in media]})


def register(app):
    @app.get("/api/assets")
    def list_assets(ctx, q, body):
        _list_bucket(ctx, "materials", q)

    @app.post("/api/assets")
    def upload_asset(ctx, q, body):
        part = (body or {}).get("file")
        if not part or not part.get("filename"):
            ctx.error("缺少 file 字段（multipart）")
        fn = os.path.basename(part["filename"])
        ext = os.path.splitext(fn)[1].lower()
        data = part.get("data") or b""
        if ext not in EXTS:
            ctx.error("不支持的素材类型 %s（支持图片 / 视频 / 音频常见格式）"
                      % (ext or "（无扩展名）"), 400)
        kind = _ext_kind(ext)
        if len(data) > _MAX_BYTES[kind]:
            ctx.error("文件大小超过 %s 类上限 %dMB"
                      % (kind, _MAX_BYTES[kind] // 1024 ** 2), 400)
        if not _head_ok(ext, data):
            ctx.error("文件内容与扩展名 %s 不符（请勿改扩展名上传）" % ext, 400)
        key = os.path.splitext(fn)[0] + ext
        try:
            o = storage.put("materials", key, data, _ctype(key))
        except storage.StorageError as e:
            ctx.error("素材保存失败: %s" % e, 500)
        try:
            # 超大参考图 → norm 副本（引擎/预览默认取小图）；失败不阻断上传
            storage.preprocess_material(key)
        except Exception as e:            # 预处理异常不影响素材入库
            LOG.warning("op=preprocess key=%s err=%s", key, e)
        ctx.json({"asset": _obj(o)})

    @app.delete("/api/assets/*")
    def delete_asset(ctx, q, body):
        key = unquote(ctx.h.path.split("?")[0][len("/api/assets/"):])
        if not key:
            ctx.error("缺少素材 key", 404)
        ctx.json({"ok": storage.delete("materials", key)})

    @app.get("/api/results")
    def list_results(ctx, q, body):
        _list_bucket(ctx, "results", q)

    @app.delete("/api/results/*")
    def delete_result(ctx, q, body):
        key = unquote(ctx.h.path.split("?")[0][len("/api/results/"):])
        if not key:
            ctx.error("缺少结果 key", 404)
        ctx.json({"ok": storage.delete("results", key)})

    @app.get("/media/*")
    def media(ctx, q, body):
        """本地文件直读（Range）。"""
        rest = unquote(ctx.h.path.split("?")[0][len("/media/"):])
        bucket, _, key = rest.partition("/")
        if bucket not in storage.BUCKETS or not key:
            ctx.error("媒体路径格式：/media/{bucket}/{key}", 404)
        try:
            path, redirect = storage.resolve_media(bucket, key)
        except storage.StorageError:
            path, redirect = None, None
        if path:
            return ctx.file(path, cache=True)
        if redirect:
            h = ctx.h
            h.send_response(302)
            h.send_header("Location", redirect)
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        ctx.error("媒体不存在: %s/%s" % (bucket, key), 404)
