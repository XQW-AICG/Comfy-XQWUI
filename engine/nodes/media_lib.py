"""media —— 素材扫描与解码/编码（仅标准库 + ffmpeg 子进程）。

图片：png/jpg/webp → 通过 _png 纯手写 PNG 解码有限支持；
      实际采用 ffmpeg 统一解码为 rawvideo（避免 PIL 依赖）。
视频：ffmpeg 解码为帧序列；音频：ffmpeg 解码为 f32le PCM。
"""
import os
import subprocess
import wave

from engine.config import temp_dir

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VID_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
AUD_EXT = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}


def _run(args, timeout=600):
    return subprocess.run(args, capture_output=True, timeout=timeout)


def ffmpeg_ok() -> bool:
    try:
        return _run(["ffmpeg", "-version"], timeout=10).returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------- 解码

def decode_image(path: str):
    """图片 → numpy (H,W,3) float32 0-1。走 ffmpeg rgb24。"""
    import uuid
    import numpy as np
    out = os.path.join(temp_dir(), "dec_%s.rgb" % uuid.uuid4().hex[:8])
    r = _run(["ffmpeg", "-y", "-v", "error", "-i", path,
              "-f", "rawvideo", "-pix_fmt", "rgb24", out], timeout=120)
    if r.returncode != 0 or not os.path.isfile(out):
        raise RuntimeError("图片解码失败: %s (%s)"
                           % (path, (r.stderr or b"").decode()[-200:]))
    data = open(out, "rb").read()
    os.remove(out)
    # rgb24 裸流无尺寸信息 → 由 ffmpeg 输出行列再解一次太浪费；
    # 直接用 ffprobe 拿宽高
    w, h = probe_size(path)
    arr = np.frombuffer(data, dtype=np.uint8)
    if w and h and arr.size >= w * h * 3:
        arr = arr[: w * h * 3].reshape(h, w, 3)
        return arr.astype("float32") / 255.0
    raise RuntimeError("图片尺寸探测失败: %s" % path)


def probe_size(path: str):
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height",
              "-of", "csv=p=0:s=x", path], timeout=30)
    try:
        w, h = r.stdout.decode().strip().split("x")
        return int(w), int(h)
    except Exception:
        return None, None


def decode_video(path: str, max_frames: int = 0, fps: float = 0.0):
    """视频 → (frames (f,H,W,3) uint8, fps)。"""
    import numpy as np
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height,avg_frame_rate,nb_frames",
              "-of", "csv=p=0:s=|", path], timeout=30)
    try:
        w_s, h_s, fr_s, nf_s = r.stdout.decode().strip().split("|")
        w, h = int(w_s), int(h_s)
        num, _, den = fr_s.partition("/")
        vfps = float(num) / float(den or 1) if den else float(num)
        nf = int(nf_s) if nf_s.isdigit() else 0
    except Exception:
        raise RuntimeError("视频信息探测失败: %s" % path)
    if fps > 0 and abs(vfps - fps) > 0.01:
        vf = "fps=%g" % fps
    else:
        vf = "null"
        vfps = fps if fps > 0 else vfps
    args = ["ffmpeg", "-v", "error", "-i", path, "-vf", vf,
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    r = _run(args + (["-frames:v", str(max_frames)]
                     if max_frames > 0 else []), timeout=1800)
    if r.returncode != 0:
        raise RuntimeError("视频解码失败: %s" % (r.stderr or b"").decode()[-200:])
    data = r.stdout
    n = len(data) // (w * h * 3)
    frames = np.frombuffer(data[: n * w * h * 3],
                           dtype=np.uint8).reshape(n, h, w, 3)
    return frames, vfps


def decode_audio(path: str, sr: int = 24000):
    """音频/视频音轨 → AUDIO dict（mono float32）。"""
    import numpy as np
    r = _run(["ffmpeg", "-v", "error", "-i", path, "-vn",
              "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"], timeout=600)
    if r.returncode != 0:
        raise RuntimeError("音频解码失败: %s" % (r.stderr or b"").decode()[-200:])
    wav = np.frombuffer(r.stdout, dtype=np.float32).reshape(1, -1)
    return {"waveform": wav, "sample_rate": sr}


# ---------------------------------------------------------------- 编码

def encode_video(frames, fps: float, out_path: str,
                 codec: str = "h264", crf: int = 18,
                 audio_path: str | None = None,
                 on_progress=None) -> str:
    """帧序列 → mp4。frames: (f,H,W,3) uint8。可选混入音轨。"""
    import numpy as np
    f, h, w, _ = frames.shape
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    vcodec = {"h264": "libx264", "h265": "libx265",
              "av1": "libsvtav1"}.get(codec, "libx264")
    args = ["ffmpeg", "-y", "-v", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", "%dx%d" % (w, h), "-r", "%g" % fps, "-i", "-"]
    if audio_path:
        args += ["-i", audio_path, "-c:a", "aac", "-b:a", "192k",
                 "-shortest"]
    args += ["-c:v", vcodec, "-crf", str(crf), "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", out_path]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    step = max(1, f // 50)
    try:
        for i in range(f):
            proc.stdin.write(np.ascontiguousarray(frames[i]).tobytes())
            if on_progress and (i % step == 0 or i == f - 1):
                on_progress(i + 1, f)
        proc.stdin.close()
        err = proc.stderr.read()
        rc = proc.wait(timeout=600)
    finally:
        try:
            proc.stderr.close()
        except Exception:
            pass
    if rc != 0:
        raise RuntimeError("视频编码失败: %s" % err.decode()[-300:])
    return out_path


def encode_audio_to_wav(audio: dict, out_path: str) -> str:
    """AUDIO dict → wav 文件。"""
    wav = audio["waveform"]
    sr = int(audio.get("sample_rate", 24000))
    import numpy as np
    data = np.clip(wav.reshape(-1), -1.0, 1.0)
    pcm = (data * 32767.0).astype("<i2").tobytes()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)
    return out_path


# ---------------------------------------------------------------- 合并 / 抽帧

def concat_videos(paths: list, out_path: str, trim_frames: list | None = None,
                  fps: float = 24.0, on_progress=None) -> str:
    """顺序拼接视频。trim_frames[i] 表示第 i 段开头丢弃的帧数（重叠去重）。

    通过 ffmpeg 逐段转中间 ts 再 concat，保证参数一致。
    """
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmps = []
    try:
        for i, p in enumerate(paths):
            trim = int(trim_frames[i]) if trim_frames and i < len(
                trim_frames) else 0
            seek = (trim / fps) if trim > 0 else 0.0
            tp = os.path.join(temp_dir(), "cat_%02d.ts" % i)
            args = ["ffmpeg", "-y", "-v", "error"]
            if seek > 0:
                args += ["-ss", "%.6f" % seek]
            args += ["-i", p, "-c", "copy", "-f", "mpegts",
                     "-r", "%g" % fps, tp]
            r = _run(args, timeout=1800)
            if r.returncode != 0:
                raise RuntimeError("片段转码失败 %s: %s"
                                   % (p, (r.stderr or b"").decode()[-200:]))
            tmps.append(tp)
            if on_progress:
                on_progress(i + 1, len(paths))
        lst = os.path.join(temp_dir(), "concat.txt")
        with open(lst, "w") as f:
            for tp in tmps:
                f.write("file '%s'\n" % tp)
        r = _run(["ffmpeg", "-y", "-v", "error", "-f", "concat",
                  "-safe", "0", "-i", lst, "-c", "copy",
                  "-movflags", "+faststart", out_path], timeout=1800)
        if r.returncode != 0:
            raise RuntimeError("拼接失败: %s" % (r.stderr or b"").decode()[-300:])
    finally:
        for tp in tmps:
            try:
                os.remove(tp)
            except OSError:
                pass
    return out_path


def extract_last_frame(video_path: str, out_png: str) -> str:
    os.makedirs(os.path.dirname(out_png) or ".", exist_ok=True)
    r = _run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.15",
              "-i", video_path, "-frames:v", "1", "-q:v", "2", out_png],
             timeout=180)
    if r.returncode != 0 or not os.path.isfile(out_png):
        raise RuntimeError("尾帧提取失败: %s"
                           % (r.stderr or b"").decode()[-200:])
    return out_png
