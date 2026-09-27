"""h3_video —— H3 长视频工作流生成器（无模板，纯代码构建）。

从任务配置（与旧控制台 cfg 同构）动态生成 Comfy API 格式工作流：

    每镜一个 r2v 组（GroupReferenceToVideo）
      → GroupsCombine（槽序连续）
      → Director（timeline_data 内联，r2v_groups 外接）
      → CreateVideo → SaveVideo（每段独立存盘时前缀带段号）

可选：加速 LoRA（LoraLoaderModelOnly）、二采 SelfLift（外接
LatentUpscaleModelLoader）。seg_index 不为 None 时只构建该段
（独立生成、独立保存；参考图 inherit 仍按全部分镜解析）。

与旧系统差异（引擎节点已适配）：
· SelfLift 的 latent_upscale_model 为 UPSCALER 链接（U 节点）
· SaveVideo 的 format/codec 为普通 combo（引擎版无 DynamicCombo）
· CreateVideo 的 fps 用字面值（引擎 Director 第 3 输出为 H3_STATE）
"""
import json
import random

from xqwui.workflow.builder import GraphBuilder
from xqwui.workflow import timeline as tl

# 引擎节点名（与 engine/nodes 注册一致，工作流 JSON 互通）
N_UNET = "UNETLoader"
N_CLIP = "CLIPLoader"
N_VAE = "VAELoader"
N_LORA = "LoraLoaderModelOnly"
N_UPSCALER = "LatentUpscaleModelLoader"
N_LOAD_IMAGE = "LoadImage"
N_LOAD_VIDEO = "LoadVideo"
N_VIDEO_COMP = "GetVideoComponents"
N_LOAD_AUDIO = "LoadAudio"
N_GROUP = "MiniMaxH3DirectorGroupReferenceToVideo"
N_COMBINE = "MiniMaxH3DirectorGroupsCombine"
N_SELF_LIFT = "MiniMaxH3DirectorSelfLift"
N_DIRECTOR = "MiniMaxH3Director"
N_CREATE_VIDEO = "CreateVideo"
N_SAVE_VIDEO = "SaveVideo"

# 最终文件编码白名单（引擎 SaveVideo.codec 实际支持集：auto/h264/av1；
# 注意引擎 CreateVideo/SaveVideo 的 COMBO 均无 h265，非法值回落 auto=保留源流）
_SAVE_CODECS = ("auto", "h264", "av1")


def _norm_codec(v, allowed, default):
    """编码值归一化：引擎 COMBO 只接受特定集合，旧配置的 h265 等回落 default"""
    s = str(v or "").strip().lower()
    return s if s in allowed else default


def _seed_of(cfg: dict) -> int:
    try:
        seed = int(cfg.get("seed", -1))
    except (TypeError, ValueError):
        seed = -1
    return seed if 0 <= seed <= 2 ** 32 - 1 else random.randrange(2 ** 32)


def _selflift_inputs(cfg: dict, upscaler_link) -> dict:
    """二采 SelfLift 节点 inputs（required 全量必提交，与插件一致）。"""
    return {
        "bd_grp_selflift_sample": "渐进采样",
        "split_mode": "highres_steps",
        "highres_steps": int(cfg.get("secondPassHighSteps", 4) or 4),
        "transition_step": 6,
        "lowres_scale": 0.5,
        "sampler_mode": "euler",
        "native_low_carry": True,
        "bd_grp_selflift_lift": "提升 / 3D",
        "latent_upscale_model": upscaler_link,
        "latent_upsample": "bilinear",
        "rho": 0.0,
        "w_min": 0.5,
        "w_max": 1.0,
        "enable_latent_chunking": False,
        "bd_grp_selflift_tile": "高清分块",
        "enable_tiling": False,
        "tile_count": 2,
        "tile_overlap": 128,
    }


def build_graph(cfg: dict, seg_index: int | None = None) -> dict:
    """任务配置 → Comfy API 格式工作流 dict（可直接 POST /prompt）。

    抛 ValueError 表示配置/结构不合法（构建期快检错误）。
    """
    segments_all = cfg.get("segments") or []
    if not segments_all:
        segments_all = [{"startFrame": 0, "endFrame": 124, "prompt": ""}]
    fps = float(cfg.get("fps", 24) or 24)
    width = tl.align32(cfg.get("width", 864))
    height = tl.align32(cfg.get("height", 480))

    # 每镜素材解析（inherit 链按全部分镜，随后按需切片）
    media_all = tl.segment_media(cfg, segments_all)
    prompts_all = [str(s.get("prompt") or "") for s in segments_all]
    if seg_index is not None:
        seg_index = max(0, min(int(seg_index), len(segments_all) - 1))
        segments = [segments_all[seg_index]]
        prompts = [prompts_all[seg_index]]
        media = [media_all[seg_index]]
    else:
        segments, prompts, media = segments_all, prompts_all, media_all

    t = tl.timing(segments, fps, single=(seg_index is not None))
    timeline_json = json.dumps(
        tl.build_timeline_data(cfg, segments, t, width, height, fps),
        ensure_ascii=False)

    b = GraphBuilder()
    # ---- 模型加载（1/2/3/4，LoRA 可选 11，二采放大模型 U）
    unet = b.add(N_UNET, {"unet_name": cfg.get("unet", ""),
                          "weight_dtype": cfg.get("weight_dtype", "default")},
                 node_id="1")
    clip = b.add(N_CLIP, {"clip_name": cfg.get("clip", ""),
                          "type": cfg.get("clip_type", "minimax"),
                          "device": cfg.get("clip_device", "default")},
                 node_id="2")
    vvae = b.add(N_VAE, {"vae_name": cfg.get("video_vae", "")}, node_id="3")
    avae = b.add(N_VAE, {"vae_name": cfg.get("audio_vae", "")}, node_id="4")
    model_out = unet.out(0)
    if str(cfg.get("lora") or "").strip():
        lora = b.add(N_LORA, {"model": unet.out(0),
                              "lora_name": cfg["lora"],
                              "strength_model":
                                  float(cfg.get("lora_strength", 1.0) or 1.0)},
                     node_id="11")
        model_out = lora.out(0)
    up_link = None
    if cfg.get("secondPass"):
        up = b.add(N_UPSCALER, {"model_name": cfg["secondPassModel"]},
                   node_id="U")
        up_link = up.out(0)
        b.add(N_SELF_LIFT, _selflift_inputs(cfg, up_link), node_id="S")

    # ---- 参考素材加载（去重保序：同文件跨镜复用只建一个加载节点）
    all_imgs, all_vids, all_auds = [], [], []
    for m in media:
        for f in m["images"]:
            if f not in all_imgs:
                all_imgs.append(f)
        for f in m["videos"]:
            if f not in all_vids:
                all_vids.append(f)
        for f in m["audios"]:
            if f not in all_auds:
                all_auds.append(f)
    img_out = {f: b.add(N_LOAD_IMAGE, {"image": f},
                        node_id="L%d" % (i + 1)).out(0)
               for i, f in enumerate(all_imgs)}
    vid_comp = {}
    for i, f in enumerate(all_vids):
        lv = b.add(N_LOAD_VIDEO, {"file": f}, node_id="V%d" % (i + 1))
        vid_comp[f] = b.add(N_VIDEO_COMP, {"video": lv.out(0)},
                            node_id="VC%d" % (i + 1))
    aud_out = {f: b.add(N_LOAD_AUDIO, {"audio": f},
                        node_id="A%d" % (i + 1)).out(0)
               for i, f in enumerate(all_auds)}

    # ---- 每镜一个 r2v 外部组（Autogrow 点号键）
    for i, m in enumerate(media):
        gin = {"prompt": prompts[i],
               "duration_sec": t["durations"][i],
               "ref_image_size": cfg.get("ref_image_size", "match")}
        for k, f in enumerate(m["images"]):
            gin["ref_images.ref_image_%d" % k] = img_out[f]
        for k, f in enumerate(m["videos"]):
            gin["ref_videos.ref_video_%d" % k] = vid_comp[f].out(0)
            gin["ref_video_audios.ref_video_audio_%d" % k] = \
                vid_comp[f].out(1)
        for k, f in enumerate(m["audios"]):
            gin["ref_audios.ref_audio_%d" % k] = aud_out[f]
        b.add(N_GROUP, gin, node_id="G%d" % (i + 1))

    # ---- 组合并（槽序从 0 连续）→ Director
    combine = b.add(N_COMBINE,
                    {"groups.group_%d" % i: b.get("G%d" % (i + 1)).out(0)
                     for i in range(len(segments))},
                    node_id="C")
    din = {
        "model": model_out,
        "clip": clip.out(0),
        "video_vae": vvae.out(0),
        "audio_vae": avae.out(0),
        "r2v_groups": combine.out(0),
        # 自带引擎用裸键（r2v）；ComfyUI 插件版 combo 为「r2v — 参考主体生视频…」
        # 完整标签 —— 由调度器按引擎 object_info 解析后经 director_task_type 注入
        "task_type": str(cfg.get("director_task_type") or "r2v"),
        "global_prompt": cfg.get("globalPrompt", ""),
        "bd_grp_sample": "采样设置",
        "cfg": 1.0,
        "seed": _seed_of(cfg),
        "frame_rate": fps,
        "width": width,
        "height": height,
        "ref_max_size": int(cfg.get("ref_max_size", 864) or 864),
        "total_frames": t["total_frames"],
        "timeline_data": timeline_json,
        "bd_grp_advanced": "高级采样",
        "steps": int(cfg.get("steps", 8) or 8),
        "sampler": cfg.get("sampler", "euler"),
        "scheduler": cfg.get("scheduler", "simple"),
        "shift_video": 12.0,
        "shift_audio": 3.0,
        "bd_grp_perf": "性能",
        "clear_vram_between_segments": True,
        "export_source_images": False,
    }
    if b.has("S"):
        din["selflift"] = b.get("S").out(0)
    director = b.add(N_DIRECTOR, din, node_id="D")

    # ---- 视频组装与存盘（9/10）
    # save_codec（auto/h264/av1）为最终文件编码：CreateVideo 的中间封装
    # 默认跟随它，避免「最终要 av1 却被中间节点压成 h264」的双重损耗；
    # 仅当显式配置 video_codec 时才覆盖中间封装。
    save_codec = str(cfg.get("save_codec", "h264") or "h264").lower()
    save_codec = _norm_codec(save_codec, _SAVE_CODECS, "auto")
    # bit_depth 引擎 COMBO 只接受 'auto'/整数 8/整数 10；
    # 字符串 '8' 会触发 400 value_not_in_list，需归一化
    bd = cfg.get("bit_depth", "auto")
    if isinstance(bd, int):
        bd = bd if bd in (8, 10) else "auto"
    else:
        s = str(bd).strip().lower()
        bd = int(s) if s in ("8", "10") else "auto"
    cv = {"images": director.out(0), "audio": director.out(1),
          "fps": fps,
          "bit_depth": bd,
          "color_space": cfg.get("color_space", "sRGB")}
    # CreateVideo 的 codec COMBO = ["none","auto","h264","av1"]（无 h265）
    if cfg.get("video_codec") and cfg["video_codec"] != "none":
        cv["codec"] = _norm_codec(cfg["video_codec"],
                                  ("auto", "h264", "av1"), "auto")
    else:
        cv["codec"] = save_codec
    create = b.add(N_CREATE_VIDEO, cv, node_id="9")

    prefix = cfg.get("filename_prefix", "xqwui/video")
    if seg_index is not None:
        prefix = "%s_s%02d" % (prefix, seg_index + 1)
    b.add(N_SAVE_VIDEO, {"video": create.out(0),
                         "filename_prefix": prefix,
                         "format": cfg.get("save_format", "mp4"),
                         "codec": save_codec},
            node_id="10")

    return b.to_api_format()


def build_segment(cfg: dict, seg_index: int) -> dict:
    """单段独立工作流（调度器按段提交的入口）。"""
    return build_graph(cfg, seg_index)


def preview(cfg: dict, seg_index: int | None = None) -> dict:
    """工作流预览：不提交执行，返回节点清单 / 时序 / 时间线摘要。"""
    cfg = tl.resolve_prompt_refs(cfg)         # @素材: 引用归并
    segments_all = cfg.get("segments") or []
    if not segments_all:
        segments_all = [{"startFrame": 0, "endFrame": 124, "prompt": ""}]
    fps = float(cfg.get("fps", 24) or 24)
    media_all = tl.segment_media(cfg, segments_all)
    if seg_index is not None:
        seg_index = max(0, min(int(seg_index), len(segments_all) - 1))
        segments = [segments_all[seg_index]]
        media = [media_all[seg_index]]
    else:
        segments, media = segments_all, media_all
    t = tl.timing(segments, fps, single=(seg_index is not None))
    graph = build_graph(cfg, seg_index)
    nodes = [{"id": nid, "class_type": nd["class_type"],
              "links": {k: v for k, v in nd["inputs"].items()
                        if isinstance(v, list) and len(v) == 2
                        and isinstance(v[0], str)}}
             for nid, nd in sorted(graph.items())]
    return {
        "segIndex": seg_index,
        "timing": t,
        "width": tl.align32(cfg.get("width", 864)),
        "height": tl.align32(cfg.get("height", 480)),
        "fps": fps,
        "media": [{"images": m["images"], "videos": m["videos"],
                   "audios": m["audios"],
                   "firstFrame": m["firstFrame"], "lastFrame": m["lastFrame"]}
                  for m in media],
        "nodeCount": len(nodes),
        "nodes": nodes,
        "timeline": tl.build_timeline_data(cfg, segments, t,
                                           tl.align32(cfg.get("width", 864)),
                                           tl.align32(cfg.get("height", 480)),
                                           fps),
    }
