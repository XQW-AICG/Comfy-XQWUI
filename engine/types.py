"""types —— 节点间流转的数据类型约定。

工作流图中节点输出直接以 Python 对象在执行器内存中传递；
这里定义每种「类型名」对应的承载结构，保证节点实现之间语义一致。

  IMAGE    numpy.ndarray  (frames, H, W, 3) float32 0-1
  AUDIO    {"waveform": numpy (channels, samples) float32,
            "sample_rate": int}
  VIDEO    {"frames": numpy (frames,H,W,3) uint8, "fps": float,
            "audio": AUDIO|None}    （内存中的成品视频）
  MODEL    {"kind": "h3", "path": str, "dtype": str,
            "loras": [{"path","strength"}]}
  CLIP     {"kind": "minimax", "path": str, "device": str}
  VAE      {"role": "video"|"audio", "path": str}
  UPSCALER {"path": str}
  GROUP    {"prompt": str, "duration_sec": float,
            "ref_images": [IMAGE], "ref_videos": [VIDEO],
            "ref_audios": [AUDIO], "ref_image_size": str}
  GROUPS   [GROUP, ...]（槽序即提交顺序）
  TIMELINE dict（version 4 时间线数据，由构建器生成）
"""

IMAGE = "IMAGE"
AUDIO = "AUDIO"
VIDEO = "VIDEO"
MODEL = "MODEL"
CLIP = "CLIP"
VAE = "VAE"
UPSCALER = "UPSCALER"
GROUP = "GROUP"
GROUPS = "GROUPS"
TIMELINE = "TIMELINE"
LATENT = "LATENT"


class NodeError(Exception):
    """节点执行错误（message 会原样进入 execution_error 事件与 history）。"""

    def __init__(self, node: str, message: str):
        super().__init__(message)
        self.node = node
        self.message = message
