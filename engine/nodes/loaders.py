"""loaders —— 模型加载节点（产出轻量 spec，真实加载在 ComputeBackend）。

与 ComfyUI 节点同名同参，保证工作流 JSON 与 Comfy 后端格式互通。
模型文件扫描 models 库的对应子目录（config.model_dirs）。
"""
import os

from engine import config as cfg
from engine.registry import node
from engine.types import MODEL, CLIP, VAE, UPSCALER

MODEL_KINDS = {
    "diffusion_models": ("UNETLoader", "unet_name"),
    "text_encoders": ("CLIPLoader", "clip_name"),
    "vae": ("VAELoader", "vae_name"),
    "loras": ("LoraLoaderModelOnly", "lora_name"),
    "latent_upscale_models": ("LatentUpscaleModelLoader", "model_name"),
}


def scan_models(kind: str) -> list:
    """扫描模型库 kind 子目录，返回相对文件名列表（含子目录）。"""
    out = []
    for base in cfg.model_subdir(kind):
        for root, _dirs, files in os.walk(base):
            for f in sorted(files):
                if f.endswith((".safetensors", ".safetensors.index.json",
                               ".pth", ".pt", ".gguf", ".ckpt")):
                    rel = os.path.relpath(os.path.join(root, f), base)
                    out.append(rel.replace(os.sep, "/"))
    return sorted(set(out))


def resolve_model(kind: str, name: str) -> str:
    """相对名 → 绝对路径；找不到抛错。"""
    if os.path.isabs(name) and os.path.isfile(name):
        return name
    for base in cfg.model_subdir(kind):
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
    raise FileNotFoundError("模型文件不存在: %s（在 %s 中未找到）"
                            % (name, kind))


def _weight_dtype_options():
    return ["default", "fp8_e4m3fn", "fp8_e4m3fn_fast", "fp8_e5m2"]


@node("UNETLoader", category="load/model")
class UNETLoader:
    """加载扩散模型（H3 系列 safetensors）。"""
    RETURN_TYPES = (MODEL,)
    RETURN_NAMES = ("model",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "unet_name": (scan_models("diffusion_models"),),
            "weight_dtype": (_weight_dtype_options(),),
        }}

    def run(self, unet_name: str, weight_dtype: str = "default"):
        return ({"kind": "h3", "path": resolve_model(
                    "diffusion_models", unet_name),
                 "dtype": weight_dtype, "loras": []},)


@node("CLIPLoader", category="load/model")
class CLIPLoader:
    """加载文本编码器（MiniMax H3 使用 Qwen3-VL 系列）。"""
    RETURN_TYPES = (CLIP,)
    RETURN_NAMES = ("clip",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "clip_name": (scan_models("text_encoders"),),
            "type": (["minimax", "wan", "sd3", "flux"],),
            "device": (["default", "cpu"],),
        }}

    def run(self, clip_name: str, type: str = "minimax",
            device: str = "default"):
        return ({"kind": type, "path": resolve_model(
            "text_encoders", clip_name), "device": device},)


@node("VAELoader", category="load/model")
class VAELoader:
    """加载 VAE（role 由用途决定：视频 / 音频）。"""
    RETURN_TYPES = (VAE,)
    RETURN_NAMES = ("vae",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "vae_name": (scan_models("vae"),),
        }}

    def run(self, vae_name: str):
        return ({"role": "video", "path": resolve_model("vae", vae_name)},)


@node("LoraLoaderModelOnly", category="load/model")
class LoraLoaderModelOnly:
    """仅作用于模型的 LoRA（H3 加速 LoRA：turbo 8step / 4step）。"""
    RETURN_TYPES = (MODEL,)
    RETURN_NAMES = ("model",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (MODEL,),
            "lora_name": (scan_models("loras"),),
            "strength_model": ("FLOAT",
                               {"default": 1.0, "min": -10.0,
                                "max": 10.0, "step": 0.05}),
        }}

    def run(self, model: dict, lora_name: str, strength_model: float = 1.0):
        m = dict(model)
        m.setdefault("loras", []).append(
            {"path": resolve_model("loras", lora_name),
             "strength": float(strength_model)})
        return (m,)


@node("LatentUpscaleModelLoader", category="load/model")
class LatentUpscaleModelLoader:
    """加载 latent 放大模型（SelfLift 二采用）。"""
    RETURN_TYPES = (UPSCALER,)
    RETURN_NAMES = ("upscaler",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model_name": (scan_models("latent_upscale_models"),),
        }}

    def run(self, model_name: str):
        return ({"path": resolve_model("latent_upscale_models", model_name)},)
