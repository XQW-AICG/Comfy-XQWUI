"""storage_api —— 本地存储路径配置。

  GET  /api/storage   各库路径与状态
  POST /api/storage   设置素材库（图片/视频/音频三分类）/ 成品库路径
                      {bucket, path}（空 = 恢复默认；保存后实时生效）
"""
import os

from xqwui import config as appcfg
from xqwui.storage import router as storage

# 可自定义路径的库：素材库三分类 + 成品库；cache 固定默认
SETTABLE = ("materials_images", "materials_videos", "materials_audios",
            "results")
_DIR_KEY = {
    "materials_images": "materials_images_dir",
    "materials_videos": "materials_videos_dir",
    "materials_audios": "materials_audios_dir",
    "results": "results_dir",
}


def _bucket_info(bucket: str) -> dict:
    h = storage.health().get(bucket) or {}
    return {"path": h.get("path") or "", "ok": bool(h.get("ok"))}


def _all_info() -> dict:
    return {b: _bucket_info(b)
            for b in ("materials", "materials_images", "materials_videos",
                      "materials_audios", "results", "cache")}


def register(app):
    @app.get("/api/storage")
    def get_storage(ctx, q, body):
        st = appcfg.load()
        ctx.json({"buckets": _all_info(),
                  "custom": {b: st.get(_DIR_KEY[b], "")
                             for b in SETTABLE}})

    @app.post("/api/storage")
    def set_storage(ctx, q, body):
        bucket = (body or {}).get("bucket")
        if bucket not in SETTABLE:
            ctx.error("不支持的存储库: %s（可配置：%s）"
                      % (bucket, " / ".join(SETTABLE)))
        raw = str(body.get("path") or "").strip()
        if raw:
            path = os.path.abspath(os.path.expanduser(raw))
            try:
                os.makedirs(path, exist_ok=True)
                if not os.access(path, os.W_OK):
                    raise OSError("目录不可写")
            except OSError as e:
                ctx.error("路径不可用: %s（%s）" % (path, e))
        else:
            path = ""                   # 恢复默认位置
        appcfg.save({_DIR_KEY[bucket]: path})
        ctx.json({"ok": True, "buckets": _all_info(),
                  "custom": {b: appcfg.load().get(_DIR_KEY[b], "")
                             for b in SETTABLE}})
