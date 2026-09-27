"""null_backend —— 占位后端：未部署真实计算栈时的清晰报错。

图执行链路本身完整可用（加载/组装/保存等节点真实执行），
一旦触达模型计算节点即抛出 BackendNotConfigured，
错误会经 execution_error 事件与 history 标准化返回给前端。
"""
from engine.compute.base import (
    ComputeBackend, BackendNotConfigured, ModelHandle, SampleRequest,
    SegmentOutput,
)


class NullBackend(ComputeBackend):
    name = "null"
    description = "未配置计算栈（占位）：模型计算节点将返回明确错误"

    @classmethod
    def available(cls) -> bool:
        return True          # 恒可用：保证引擎链路可演示

    def _raise(self):
        raise BackendNotConfigured()

    def load_unet(self, path, dtype="default") -> ModelHandle:
        self._raise()

    def load_clip(self, path, kind="minimax", device="default") -> ModelHandle:
        self._raise()

    def load_vae(self, path, role) -> ModelHandle:
        self._raise()

    def load_upscaler(self, path) -> ModelHandle:
        self._raise()

    def apply_lora(self, model, path, strength) -> ModelHandle:
        self._raise()

    def sample_segment(self, req: SampleRequest, on_progress=None,
                       on_preview=None, check_interrupt=None) -> SegmentOutput:
        self._raise()

    def free(self, handle=None):
        pass
