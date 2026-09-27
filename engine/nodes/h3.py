"""h3 —— MiniMax H3 导演台节点（引擎版）。

节点名与 AIMixer 插件保持一致，工作流 JSON 结构互通：
  MiniMaxH3DirectorGroupReferenceToVideo   一个 r2v 分镜组
  MiniMaxH3DirectorGroupsCombine           组合并（Autogrow groups）
  MiniMaxH3Director                        核心推理（委托 ComputeBackend）
  MiniMaxH3DirectorSelfLift                二采规格（输出 SelfLiftSpec）
  MiniMaxH3LatentUpscalerModel             二采模型加载（别名节点）

模型计算（文本编码/采样/解码）全部经 engine.compute.get_backend() 委托；
未部署计算栈时 Director 抛出 BackendNotConfigured，标准化返回前端。
"""
import json

from engine.compute import get_backend, BackendNotConfigured
from engine.compute.base import (
    Group as BGroup, GroupRef, SelfLiftSpec, SampleRequest)
from engine.registry import node
from engine.types import (
    IMAGE, AUDIO, VIDEO, MODEL, CLIP, VAE, UPSCALER, GROUP, GROUPS,
    NodeError,
)


@node("MiniMaxH3DirectorGroupReferenceToVideo", category="h3/group")
class H3GroupReferenceToVideo:
    """一个 r2v 组：提示词 + 时长 + 参考图/视频/音频（Autogrow 键传入）。"""
    RETURN_TYPES = (GROUP,)
    RETURN_NAMES = ("group",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "prompt": ("STRING", {"default": ""}),
            "duration_sec": ("FLOAT", {"default": 5.17, "min": 0.2,
                                       "max": 60.0}),
            "ref_image_size": (["match", "1024", "864", "768", "640",
                                "512"],),
        },
            "optional": {
                "ref_images": ("AUTOGROW",),
                "ref_videos": ("AUTOGROW",),
                "ref_video_audios": ("AUTOGROW",),
                "ref_audios": ("AUTOGROW",),
            }}

    def run(self, prompt="", duration_sec=5.17, ref_image_size="match",
            ref_images=None, ref_videos=None, ref_video_audios=None,
            ref_audios=None):
        imgs = _ordered(ref_images)
        vids = _ordered(ref_videos)
        vauds = _ordered(ref_video_audios)
        auds = _ordered(ref_audios)
        # 参考视频的音轨与画面按序对齐（点号键 ref_video_audio_k）
        paired = []
        for i, v in enumerate(vids):
            a = vauds[i] if i < len(vauds) else None
            paired.append((v, a))
        if not prompt or not str(prompt).strip():
            raise NodeError(self.__class__.__name__,
                            "r2v 组提示词为空（每段都必须有提示词）")
        return (BGroup(
            prompt=str(prompt), duration_sec=float(duration_sec),
            refs=GroupRef(images=list(imgs), videos=[v for v, _ in paired],
                          audios=list(auds)),
        ),)


def _ordered(d: dict | None) -> list:
    """点号键 dict → 按数字后缀排序的值列表。"""
    if not d:
        return []
    def key(kv):
        import re
        m = re.search(r"(\d+)$", kv[0])
        return (int(m.group(1)) if m else 0, kv[0])
    return [v for _k, v in sorted(d.items(), key=key)]


@node("MiniMaxH3DirectorGroupsCombine", category="h3/group")
class H3GroupsCombine:
    """合并多个组（Autogrow groups.group_N），槽序必须从 0 连续。"""
    RETURN_TYPES = (GROUPS,)
    RETURN_NAMES = ("groups",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {},
                "optional": {"groups": ("AUTOGROW",)}}

    def run(self, groups=None):
        gs = _ordered(groups)
        if not gs:
            raise NodeError(self.__class__.__name__, "没有可合并的分镜组")
        return (list(gs),)


@node("MiniMaxH3DirectorSelfLift", category="h3/refine")
class H3SelfLift:
    """二采规格：低清采样 → latent 放大 → 高清精修（两阶段固定 euler）。"""
    RETURN_TYPES = ("SELFLIFT",)
    RETURN_NAMES = ("selflift",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "bd_grp_selflift_sample": (["渐进采样"],),
            "split_mode": (["highres_steps", "transition_step"],),
            "highres_steps": ("INT", {"default": 4, "min": 1, "max": 32}),
            "transition_step": ("INT", {"default": 6, "min": 0, "max": 64}),
            "lowres_scale": ("FLOAT", {"default": 0.5, "min": 0.1,
                                       "max": 1.0}),
            "sampler_mode": (["euler"],),
            "native_low_carry": ([True, False],),
            "bd_grp_selflift_lift": (["提升 / 3D"],),
            "latent_upscale_model": (UPSCALER,),
            "latent_upsample": (["bilinear", "nearest"],),
            "rho": ("FLOAT", {"default": 0.0}),
            "w_min": ("FLOAT", {"default": 0.5}),
            "w_max": ("FLOAT", {"default": 1.0}),
            "enable_latent_chunking": ([True, False],),
            "bd_grp_selflift_tile": (["高清分块"],),
            "enable_tiling": ([True, False],),
            "tile_count": ("INT", {"default": 2}),
            "tile_overlap": ("INT", {"default": 128}),
        }}

    def run(self, split_mode="highres_steps", highres_steps=4,
            transition_step=6, lowres_scale=0.5,
            latent_upscale_model=None, **_kw):
        up = latent_upscale_model or {}
        if not up.get("path"):
            raise NodeError(self.__class__.__name__,
                            "二采已开启但未选择 latent 放大模型")
        return (SelfLiftSpec(
            enabled=True, upscaler_path=up["path"],
            highres_steps=int(highres_steps),
            transition_step=int(transition_step),
            lowres_scale=float(lowres_scale)),)


@node("MiniMaxH3Director", category="h3")
class H3Director:
    """H3 导演台核心：组参考 + 时间线 → 采样 → 视频/音频 latent 解码。

    实际张量计算委托 ComputeBackend.sample_segment；
    节点负责组装请求、进度与中断、标准化产出 (frames, audio, VIDEO)。
    """
    RETURN_TYPES = (IMAGE, AUDIO, "H3_STATE")
    RETURN_NAMES = ("images", "audio", "state")
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (MODEL,),
            "clip": (CLIP,),
            "video_vae": (VAE,),
            "audio_vae": (VAE,),
        },
            "optional": {
                "r2v_groups": (GROUPS,),
                "selflift": ("SELFLIFT",),
                "task_type": (["r2v", "flf2v", "t2v", "i2v"],),
                "global_prompt": ("STRING", {"default": ""}),
                "bd_grp_sample": (["采样设置"],),
                "cfg": ("FLOAT", {"default": 1.0, "min": 1.0, "max": 12.0}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 2 ** 32 - 1}),
                "frame_rate": ("FLOAT", {"default": 24.0, "min": 1.0,
                                         "max": 120.0}),
                "width": ("INT", {"default": 864, "min": 32, "max": 4096,
                                  "step": 32}),
                "height": ("INT", {"default": 480, "min": 32, "max": 4096,
                                   "step": 32}),
                "ref_max_size": ("INT", {"default": 864, "min": 256,
                                         "max": 2048}),
                "total_frames": ("INT", {"default": 124, "min": 5}),
                "timeline_data": ("STRING", {"default": "{}"}),
                "bd_grp_advanced": (["高级采样"],),
                "steps": ("INT", {"default": 8, "min": 1, "max": 64}),
                "sampler": (["euler", "euler_ancestral", "res_multistep"],),
                "scheduler": (["simple", "normal", "beta", "karras"],),
                "shift_video": ("FLOAT", {"default": 12.0}),
                "shift_audio": ("FLOAT", {"default": 3.0}),
                "bd_grp_perf": (["性能"],),
                "clear_vram_between_segments": ([True, False],),
                "export_source_images": ([True, False],),
            }}

    def run(self, model, clip, video_vae, audio_vae, task_type="r2v",
            global_prompt="", cfg=1.0, seed=0, frame_rate=24.0,
            width=864, height=480, ref_max_size=864, total_frames=124,
            timeline_data="{}", steps=8, sampler="euler",
            scheduler="simple", shift_video=12.0, shift_audio=3.0,
            r2v_groups=None, selflift=None, _ctx=None, **_kw):
        try:
            backend = get_backend()
        except BackendNotConfigured as e:
            raise NodeError(self.__class__.__name__, str(e))
        groups = list(r2v_groups or [])
        if not groups:
            raise NodeError(self.__class__.__name__,
                            "没有分镜组（r2v_groups 为空）")
        try:
            tl = json.loads(timeline_data or "{}")
        except Exception:
            tl = {}
        # 与插件一致：timeline.global.commonEnabled 为真时把全局提示词拼到
        # 每段提示词开头（全局 + 空行 + 本段），否则仅作空提示兜底
        gblock = (tl.get("global") or {})
        gp = str(global_prompt or gblock.get("prompt") or "").strip()
        if gp and bool(gblock.get("commonEnabled")):
            for g in groups:
                seg = str(getattr(g, "prompt", "") or "").strip()
                g.prompt = ("%s\n\n%s" % (gp, seg)) if seg else gp
        cont = (tl.get("output") or {})
        req = SampleRequest(
            task_type=task_type,
            groups=groups,
            global_prompt=global_prompt or "",
            width=int(width), height=int(height),
            fps=float(frame_rate), steps=int(steps), cfg=float(cfg),
            seed=int(seed), sampler=sampler, scheduler=scheduler,
            shift_video=float(shift_video), shift_audio=float(shift_audio),
            continuity_enabled=bool(cont.get("continuityEnabled")),
            continuity_frames=int(cont.get("continuityOverlapFrames") or 0),
            selflift=selflift or SelfLiftSpec(),
            timeline_data=tl,
            model=model, clip=clip,
            vae_video=video_vae, vae_audio=audio_vae,
        )
        out = backend.sample_segment(
            req,
            on_progress=(lambda c, t: _ctx.report(c, t) if _ctx else None),
            check_interrupt=(lambda: bool(_ctx.check_interrupt())
                             if _ctx else False),
        )
        frames = getattr(out, "frames", None)
        audio = getattr(out, "audio", None) or \
            {"waveform": None, "sample_rate": 24000}
        state = {"meta": getattr(out, "meta", {}),
                 "seed": int(seed), "task_type": task_type,
                 "segments": len(groups)}
        return (frames, audio, state)
