"""base —— 计算后端抽象接口与数据结构。

实现方职责（以 MiniMax H3 为例）：
  · load_*      从 safetensors 路径加载模型，返回句柄（形状不限）
  · encode_prompt   文本（+参考图）→ 编码上下文
  · sample_segment  一次分段级采样（含组参考、连续性引导、可选二采）
  · decode_video / decode_audio  latent → 帧 / 波形
  · free        释放句柄与显存

所有回调约定：
  on_progress(cur, total, stage)     stage ∈ encode/sample/decode/upscale
  on_preview(jpeg_bytes)             采样预览帧（可不做）
  check_interrupt() -> bool          每步采样前调用，True 则中止
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ComputeBackendError(Exception):
    """计算后端执行错误。"""


class BackendNotConfigured(ComputeBackendError):
    MESSAGE = (
        "计算后端未配置：引擎的模型计算（扩散采样 / 文本编码 / VAE 解码）"
        "由可插拔 ComputeBackend 承担。请在 engine/compute/backends/ 下"
        "部署实现了 engine.compute.base.ComputeBackend 的后端，"
        "或在 data/engine/config.json 中将 compute_backend 指向可用后端名。"
    )

    def __init__(self, detail: str = ""):
        super().__init__(
            self.MESSAGE + (("\n后端详情: " + detail) if detail else ""))


@dataclass
class ModelHandle:
    """已加载模型的轻量句柄（内容由后端自定义）。"""
    kind: str                 # unet / clip / vae-video / vae-audio / upscaler
    path: str
    obj: object = None        # 后端私有对象
    meta: dict = field(default_factory=dict)


@dataclass
class GroupRef:
    """r2v 组的参考素材（节点层已解码为内存对象）。"""
    images: list = field(default_factory=list)     # [np.ndarray HWC float 0-1]
    videos: list = field(default_factory=list)     # [np.ndarray (f,H,W,3)]
    audios: list = field(default_factory=list)     # [dict waveform/sr]


@dataclass
class Group:
    """一个 r2v 分镜组。"""
    prompt: str = ""
    duration_sec: float = 0.0
    refs: GroupRef = field(default_factory=GroupRef)


@dataclass
class SelfLiftSpec:
    """二采（SelfLift 渐进分辨率）配置。"""
    enabled: bool = False
    upscaler_path: str = ""
    highres_steps: int = 4
    transition_step: int = 6
    lowres_scale: float = 0.5


@dataclass
class SampleRequest:
    """一次分段采样请求（节点层组装，后端实现）。"""
    task_type: str = "r2v"                  # 预留：r2v / flf2v / t2v
    groups: list = field(default_factory=list)          # [Group]
    global_prompt: str = ""
    width: int = 864
    height: int = 480
    fps: float = 24.0
    steps: int = 8
    cfg: float = 1.0
    seed: int = 0
    sampler: str = "euler"
    scheduler: str = "simple"
    shift_video: float = 12.0
    shift_audio: float = 3.0
    continuity_enabled: bool = False
    continuity_frames: int = 0
    selflift: SelfLiftSpec = field(default_factory=SelfLiftSpec)
    timeline_data: dict = field(default_factory=dict)
    # 依赖句柄（由 Director 节点从 loader 输出接入）
    model: ModelHandle | None = None
    clip: ModelHandle | None = None
    vae_video: ModelHandle | None = None
    vae_audio: ModelHandle | None = None


@dataclass
class SegmentOutput:
    """分段采样产物（后端 → Director 节点）。"""
    frames: object = None        # np.ndarray (f,H,W,3) uint8
    audio: dict | None = None    # AUDIO 结构
    meta: dict = field(default_factory=dict)


class ComputeBackend(ABC):
    """模型计算后端抽象。实现必须无参构造。"""

    name: str = "abstract"
    description: str = ""

    # ---- 能力

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """当前环境是否可运行（依赖、设备检查）。"""

    def probe(self) -> dict:
        """能力/设备详情（/system_stats 汇聚展示）。"""
        return {"name": self.name, "available": True}

    # ---- 模型加载

    @abstractmethod
    def load_unet(self, path: str, dtype: str = "default") -> ModelHandle: ...

    @abstractmethod
    def load_clip(self, path: str, kind: str = "minimax",
                  device: str = "default") -> ModelHandle: ...

    @abstractmethod
    def load_vae(self, path: str, role: str) -> ModelHandle: ...

    @abstractmethod
    def load_upscaler(self, path: str) -> ModelHandle: ...

    @abstractmethod
    def apply_lora(self, model: ModelHandle, path: str,
                   strength: float) -> ModelHandle: ...

    # ---- 推理

    @abstractmethod
    def sample_segment(self, req: SampleRequest,
                       on_progress=None, on_preview=None,
                       check_interrupt=None) -> SegmentOutput:
        """执行一次分段级推理，返回帧与音频。"""

    # ---- 资源

    @abstractmethod
    def free(self, handle: ModelHandle | None = None):
        """释放句柄；handle 为 None 时释放全部缓存。"""

    def memory_stats(self) -> dict:
        return {}
