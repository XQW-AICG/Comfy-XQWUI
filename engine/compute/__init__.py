"""compute —— 可插拔计算后端。

引擎的模型计算（扩散采样 / 文本编码 / VAE 解码 / latent 放大）全部
委托给 ComputeBackend 实现。引擎本体不内置任何具体模型计算，
遵循「协议与调度归引擎、张量计算归后端」的分层：

  engine/compute/base.py        抽象接口 + 请求/结果数据结构
  engine/compute/backends/*.py  具体后端（每文件暴露 BACKEND 类）
  engine/compute/loader.py      发现与选择后端（配置 compute_backend）

新增后端：在 backends/ 下放一个实现 ComputeBackend 的类并以
BACKEND = MyBackend 命名导出即可被 auto 模式发现。
"""
from engine.compute.base import (
    ComputeBackend, ComputeBackendError, BackendNotConfigured,
    ModelHandle, SampleRequest, SegmentOutput,
)
from engine.compute.loader import get_backend, list_backends

__all__ = [
    "ComputeBackend", "ComputeBackendError", "BackendNotConfigured",
    "ModelHandle", "SampleRequest", "SegmentOutput",
    "get_backend", "list_backends",
]
