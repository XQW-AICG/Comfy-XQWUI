"""media —— 素材 IO 节点。

LoadImage / LoadVideo / GetVideoComponents / LoadAudio 从引擎 input 目录
读取素材（应用后端通过 /upload/image 或直接落盘同步）；
SaveImage / SaveAudio 写入 output 目录并产出 ui 引用。
"""
import os
import uuid

from engine import config as cfg
from engine.registry import node
from engine.types import IMAGE, AUDIO, VIDEO, NodeError
from engine.nodes import media_lib


def _input_path(name: str) -> str:
    p = os.path.realpath(os.path.join(cfg.input_dir(), name))
    if not p.startswith(os.path.realpath(cfg.input_dir()) + os.sep):
        raise NodeError("media", "素材路径越界: %s" % name)
    if not os.path.isfile(p):
        raise NodeError("media", "素材不存在: %s" % name)
    return p


def _ext_of(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def image_options() -> list:
    d = cfg.input_dir()
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d)
                  if _ext_of(f) in media_lib.IMG_EXT)


def video_options() -> list:
    d = cfg.input_dir()
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d)
                  if _ext_of(f) in media_lib.VID_EXT)


def audio_options() -> list:
    d = cfg.input_dir()
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d)
                  if _ext_of(f) in media_lib.AUD_EXT)


def _save_output(data: bytes, prefix: str, ext: str) -> dict:
    """写入 output 目录（prefix 支持子目录），返回 ui 引用。"""
    safe_prefix = prefix.strip().replace("\\", "/").strip("/")
    safe_prefix = os.path.join(*[p for p in safe_prefix.split("/")
                                 if p and p not in (".", "..")]) \
        if safe_prefix else "misc"
    stem = os.path.basename(safe_prefix)
    sub = os.path.dirname(safe_prefix)
    outd = os.path.join(cfg.output_dir(), sub)
    os.makedirs(outd, exist_ok=True)
    fn = "%s_%s%s" % (stem, uuid.uuid4().hex[:8], ext)
    full = os.path.join(outd, fn)
    with open(full, "wb") as f:
        f.write(data)
    return {"filename": fn, "subfolder": sub, "type": "output"}


@node("LoadImage", category="media")
class LoadImage:
    """从输入库加载参考图 → IMAGE (H,W,3) float32 0-1。"""
    RETURN_TYPES = (IMAGE,)
    RETURN_NAMES = ("image",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": (image_options(),)}}

    def run(self, image: str):
        return (media_lib.decode_image(_input_path(image)),)


@node("LoadVideo", category="media")
class LoadVideo:
    """从输入库加载视频 → VIDEO（解码为帧 + fps + 音轨）。"""
    RETURN_TYPES = (VIDEO,)
    RETURN_NAMES = ("video",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"file": (video_options(),)}}

    def run(self, file: str):
        p = _input_path(file)
        frames, fps = media_lib.decode_video(p)
        audio = None
        try:
            audio = media_lib.decode_audio(p)
        except Exception:
            pass
        return ({"frames": frames, "fps": fps, "audio": audio},)


@node("GetVideoComponents", category="media")
class GetVideoComponents:
    """拆分视频为画面帧批次与音轨（参考视频走此路径）。"""
    RETURN_TYPES = (IMAGE, AUDIO)
    RETURN_NAMES = ("image", "audio")
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"video": (VIDEO,)}}

    def run(self, video: dict):
        frames = video.get("frames")
        if frames is None:
            raise NodeError(self.__class__.__name__, "VIDEO 缺少 frames")
        img = frames.astype("float32") / 255.0
        audio = video.get("audio") or \
            {"waveform": None, "sample_rate": 24000}
        return (img, audio)


@node("LoadAudio", category="media")
class LoadAudio:
    """从输入库加载音频 → AUDIO。"""
    RETURN_TYPES = (AUDIO,)
    RETURN_NAMES = ("audio",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"audio": (audio_options(),)}}

    def run(self, audio: str):
        return (media_lib.decode_audio(_input_path(audio)),)


@node("SaveImage", category="media/output", )
class SaveImage:
    """保存 IMAGE 到输出库（PNG）。OUTPUT_NODE。"""
    RETURN_TYPES = ("UI",)
    OUTPUT_NODE = True
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "images": (IMAGE,),
            "filename_prefix": ("STRING", {"default": "engine/image"}),
        }}

    def run(self, images, filename_prefix="engine/image", _ctx=None):
        import numpy as np
        arr = images
        if arr.ndim == 3:
            arr = arr[None]
        ui = {"images": []}
        for i in range(arr.shape[0]):
            frame = (np.clip(arr[i], 0, 1) * 255).astype("uint8")
            import io
            buf = io.BytesIO()
            _write_png(buf, frame)
            ui["images"].append(
                _save_output(buf.getvalue(), "%s_%05d" % (filename_prefix, i),
                             ".png"))
        return ({"ui": ui},)


def _write_png(buf, rgb: "np.ndarray"):
    """最小 PNG 编码（标准库 zlib）。rgb: (H,W,3) uint8。"""
    import struct
    import zlib
    h, w, _ = rgb.shape

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))
    buf.write(b"\x89PNG\r\n\x1a\n")
    buf.write(chunk(b"IHDR", ihdr))
    buf.write(chunk(b"IDAT", zlib.compress(raw, 6)))
    buf.write(chunk(b"IEND", b""))


@node("SaveAudio", category="media/output")
class SaveAudio:
    """保存 AUDIO 到输出库（WAV）。OUTPUT_NODE。"""
    RETURN_TYPES = ("UI",)
    OUTPUT_NODE = True
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "audio": (AUDIO,),
            "filename_prefix": ("STRING", {"default": "engine/audio"}),
        }}

    def run(self, audio: dict, filename_prefix="engine/audio", _ctx=None):
        import io
        import wave
        import numpy as np
        buf = io.BytesIO()
        data = np.clip(audio["waveform"].reshape(-1), -1.0, 1.0)
        pcm = (data * 32767.0).astype("<i2").tobytes()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(audio.get("sample_rate", 24000)))
            wf.writeframes(pcm)
        ui = {"audio": [_save_output(buf.getvalue(), filename_prefix, ".wav")]}
        return ({"ui": ui},)
