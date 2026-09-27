"""nodes —— 引擎节点实现（导入即注册）。

模块划分：
  loaders  模型加载（UNET/CLIP/VAE/LoRA/latent upscaler）→ 轻量 spec 句柄
  media    素材 IO（LoadImage / LoadVideo / GetVideoComponents / LoadAudio /
           SaveImage / SaveAudio / LoadImageOutput）
  video    视频组装与成品保存（CreateVideo / SaveVideo / MergeVideos /
           ExtractLastFrame）
  h3       H3 导演台节点（r2v 组 / 组合并 / Director / SelfLift），
           模型计算委托 ComputeBackend
"""
from engine.nodes import loaders, media, video, h3   # noqa: F401
