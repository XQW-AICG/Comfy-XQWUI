"""video —— 视频组装与成品保存节点。

CreateVideo   帧 + 音轨 → 内存 VIDEO（兼容 ComfyUI CreateVideo 语义）
SaveVideo     VIDEO → mp4 落盘（OUTPUT_NODE），ui 返回 filename/subfolder
MergeVideos   多段视频按序拼接（重叠帧裁剪），长片合批必需
ExtractLastFrame  取视频尾帧为 IMAGE（接力生成）
"""
import os
import uuid

from engine import config as cfg
from engine.registry import node
from engine.types import IMAGE, AUDIO, VIDEO, NodeError
from engine.nodes import media_lib


def _output_full(ref: dict) -> str:
    """ui 引用 → 绝对路径（校验边界）。"""
    sub = (ref or {}).get("subfolder", "")
    fn = (ref or {}).get("filename", "")
    root = os.path.realpath(cfg.output_dir())
    p = os.path.realpath(os.path.join(root, sub, fn))
    if not p.startswith(root + os.sep):
        raise NodeError("video", "输出路径越界")
    return p


@node("CreateVideo", category="video")
class CreateVideo:
    """帧序列 + 音轨 + 帧率 → 内存 VIDEO。"""
    RETURN_TYPES = (VIDEO,)
    RETURN_NAMES = ("video",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "images": (IMAGE,),
            "audio": (AUDIO,),
            "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 120.0}),
        },
            "optional": {
                "bit_depth": (["8", "16"],),
                "color_space": (["sRGB", "BT.709"],),
                "codec": (["none", "h264", "h265", "av1"],),
            }}

    def run(self, images, audio, fps=24.0, bit_depth="8",
            color_space="sRGB", codec="none"):
        import numpy as np
        frames = images
        if frames.ndim == 3:
            frames = frames[None]
        frames = (np.clip(frames, 0, 1) * 255).astype("uint8")
        return ({"frames": frames, "fps": float(fps), "audio": audio},)


@node("SaveVideo", category="video/output")
class SaveVideo:
    """VIDEO → mp4 写入输出库。OUTPUT_NODE。"""
    RETURN_TYPES = ("UI",)
    OUTPUT_NODE = True
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "video": (VIDEO,),
            "filename_prefix": ("STRING", {"default": "engine/video"}),
            "format": (["auto", "mp4", "webm", "mkv"],),
            "codec": (["auto", "h264", "av1", "h265", "prores"],),
        }}

    def run(self, video: dict, filename_prefix="engine/video",
            format="mp4", codec="h264", _ctx=None):
        frames = video.get("frames")
        if frames is None or getattr(frames, "size", 0) == 0:
            raise NodeError(self.__class__.__name__,
                            "VIDEO 无画面帧，无法保存")
        fps = float(video.get("fps") or 24.0)
        fmt = format if format != "auto" else "mp4"
        cod = codec if codec != "auto" else "h264"
        # 一次性编码（进度回调 → 引擎进度）
        tmp = os.path.join(cfg.temp_dir(),
                           "sv_%s.mp4" % uuid.uuid4().hex[:8])
        audio_path = None
        audio = video.get("audio")
        if audio and audio.get("waveform") is not None and \
                audio["waveform"].size > 0:
            audio_path = os.path.join(cfg.temp_dir(),
                                      "sv_%s.wav" % uuid.uuid4().hex[:8])
            media_lib.encode_audio_to_wav(audio, audio_path)
        media_lib.encode_video(
            frames, fps, tmp, codec=cod,
            audio_path=audio_path,
            on_progress=(lambda c, t: _ctx.report(c, t)) if _ctx else None)
        with open(tmp, "rb") as f:
            data = f.read()
        os.remove(tmp)
        if audio_path:
            try:
                os.remove(audio_path)
            except OSError:
                pass
        ref = _save_video_output(data, filename_prefix, "." + fmt)
        return ({"ui": {"video": [ref],
                        "filenames": ["%s/%s" % (ref["subfolder"],
                                                 ref["filename"])]}},)


def _save_video_output(data: bytes, prefix: str, ext: str) -> dict:
    safe = prefix.strip().replace("\\", "/").strip("/")
    parts = [p for p in safe.split("/") if p and p not in (".", "..")]
    prefix_clean = "/".join(parts) if parts else "misc"
    stem = os.path.basename(prefix_clean)
    sub = os.path.dirname(prefix_clean)
    outd = os.path.join(cfg.output_dir(), sub)
    os.makedirs(outd, exist_ok=True)
    fn = "%s_%s%s" % (stem, uuid.uuid4().hex[:8], ext)
    with open(os.path.join(outd, fn), "wb") as f:
        f.write(data)
    return {"filename": fn, "subfolder": sub, "type": "output"}


@node("MergeVideos", category="video/output")
class MergeVideos:
    """按序拼接多段 mp4；trim_start_frames 可裁掉每段开头重叠帧。"""
    RETURN_TYPES = (VIDEO,)
    RETURN_NAMES = ("video",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "videos": ("VIDEO_LIST",),
            "trim_start_frames": ("STRING",
                                  {"default": ""}),   # 逗号分隔，与段一一对应
        },
            "optional": {"filename_prefix": ("STRING",
                                             {"default": "engine/merged"})}}

    def run(self, videos, trim_start_frames="", filename_prefix="engine/merged",
            _ctx=None):
        paths = []
        fps = 24.0
        for v in videos:
            p = _output_full(v.get("file") or v)
            paths.append(p)
            fps = float(v.get("fps") or fps) if isinstance(v, dict) else fps
        trims = []
        for s in (trim_start_frames or "").split(","):
            s = s.strip()
            trims.append(int(float(s)) if s else 0)
        tmp = os.path.join(cfg.temp_dir(), "mg_%s.mp4" % uuid.uuid4().hex[:8])
        media_lib.concat_videos(paths, tmp, trims, fps,
                                on_progress=(lambda c, t: _ctx.report(c, t))
                                if _ctx else None)
        with open(tmp, "rb") as f:
            data = f.read()
        os.remove(tmp)
        ref = _save_video_output(data, filename_prefix, ".mp4")
        return ({"frames": None, "fps": fps, "audio": None,
                 "file": {"filename": ref["filename"],
                          "subfolder": ref["subfolder"],
                          "type": "output", "fps": fps}},)
