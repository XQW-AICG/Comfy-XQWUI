"""timeline —— 时间线规则与 timeline_data v4 结构（移植旧系统语义）。

规则清单（与 AIMixer 插件 / 旧控制台一致）：
· 分辨率对齐 32 的倍数（H3 约束）
· 单段帧网格 5+17n（snap_frames，向上取）
· 段间引导帧就近吸附 {5, 22, 39, 56, ...}（snap_continuity，并列取大）
· 段时长 = 「新增帧」= endFrame_i - endFrame_{i-1}（首镜 = endFrame_0）；
  单段独立生成时改为整段长度
· 参考图 inherit 链：某镜未指定参考图时沿用上一镜的有效参考图
· 参考素材上限：图片不限；视频 / 音频各 ≤9；首帧 / 尾帧素材各 ≤1
· 提示词支持 @素材:文件名 引用记号（图片/音频/视频），提交前解析归并
"""
import copy
import json
import os
import re
from typing import Any

GRID_BASE, GRID_STEP = 5, 17
# 段间引导帧候选（5+17k），snap 时就近、并列取大
CONT_CANDIDATES = tuple(GRID_BASE + GRID_STEP * k for k in range(24))
MAX_REF = {"videos": 9, "audios": 9}   # 图片参考不设上限；视频 / 音频各 ≤9
MAX_FL = 1                       # 首帧 / 尾帧素材各最多 1 个


# ---------------------------------------------------------------- 网格
def align32(v: Any, lo: int = 32) -> int:
    """分辨率对齐到 32 的倍数（H3 要求）。"""
    try:
        return max(lo, int(round(float(v) / 32.0)) * 32)
    except (TypeError, ValueError):
        return lo


def snap_frames(n: Any) -> int:
    """向上 snap 到 MiniMax 帧网格（5+17n）。"""
    n = max(GRID_BASE, int(n or GRID_BASE))
    while n % GRID_STEP != GRID_BASE:
        n += 1
    return n


def snap_continuity(n: Any) -> int:
    """段间引导帧数 snap：就近、并列取大。"""
    n = int(n or 0)
    return min(CONT_CANDIDATES, key=lambda g: (abs(g - n), -g))


# ---------------------------------------------------------------- 素材
def ref_files_of(cfg: dict[str, Any], segments: list[dict[str, Any]]) -> list[list[str]]:
    """每镜的有效参考图文件名列表（inherit 链解析）。

    前端以素材 id 列表下发每镜 images；id → 文件名查 cfg["images"]。
    空列表时优先用本镜首帧素材充当参考图（首帧即画面起点，可作
    inherit 链起点 / 接力），仍为空才沿用上一镜的有效列表。
    """
    file_of: dict[Any, Any] = {im.get("id"): im.get("file")
                               for im in (cfg.get("images") or [])
                               if im.get("file")}
    out: list[list[str]] = []
    prev: list[str] = []
    for s in segments:
        refs: list[str] = [file_of[i] for i in (s.get("images") or [])
                           if i in file_of]
        if not refs:
            refs = [str(x) for x in (s.get("firstFrame") or []) if str(x).strip()]
        if not refs:
            refs = list(prev)
        out.append(refs)
        if refs:
            prev = refs
    return out


def segment_media(cfg: dict[str, Any],
                  segments: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """每镜参考素材：{"images","videos","audios","firstFrame","lastFrame"}。

    均为文件名（含截断）。图片走 inherit 链（本镜无图时首帧素材可
    充当参考图，见 ref_files_of）；视频 / 音频 / 首尾帧由前端按段下发
    文件名列表（首尾帧独立字段）。
    """
    segs = segments if segments is not None else cfg.get("segments") or []
    imgs = ref_files_of(cfg, segs)
    out: list[dict[str, Any]] = []
    for i, s in enumerate(segs):
        vids = [str(x) for x in (s.get("refVideos") or []) if x][:MAX_REF["videos"]]
        auds = [str(x) for x in (s.get("refAudios") or []) if x][:MAX_REF["audios"]]
        ffs = [str(x) for x in (s.get("firstFrame") or []) if x][:MAX_FL]
        lfs = [str(x) for x in (s.get("lastFrame") or []) if x][:MAX_FL]
        own = imgs[i] if i < len(imgs) else []
        out.append({
            "images": [x for x in own if x],   # 图片参考不设上限
            "videos": vids,
            "audios": auds,
            "firstFrame": ffs,
            "lastFrame": lfs,
        })
    return out


# ---------------------------------------------------------------- 时序
def timing(segments: list[dict[str, Any]], fps: float,
           single: bool = False) -> dict[str, Any]:
    """计算每镜时长与段间引导。

    durations  每镜秒数（「新增帧」模型；single=True 时为整段长度）
    snapped    每镜 snap 后帧数（5+17n）
    ovls       每镜与前镜的重叠帧（首镜 0）
    cont_on    是否启用段间引导（任一重叠 ≥1）
    cont_frames 吸附后的段间引导帧数（未启用为 0）
    """
    durations, ovls, snapped = [], [], []
    prev_end = 0
    for i, s in enumerate(segments):
        start, end = int(s["startFrame"]), int(s["endFrame"])
        if single:
            start, end, prev_end = start, end, start
        delta = max(1, end - prev_end)
        durations.append(round(delta / float(fps), 2))
        snapped.append(snap_frames(delta))
        ovls.append(max(0, prev_end - start) if i > 0 else 0)
        prev_end = end
    cont_on = any(o >= 1 for o in ovls)
    cont_frames = snap_continuity(max(ovls)) if cont_on else 0
    return {"durations": durations, "snapped": snapped, "ovls": ovls,
            "cont_on": cont_on, "cont_frames": cont_frames,
            "total_frames": sum(snapped)}


# ---------------------------------------------------------------- 校验
def validate_segments(cfg: dict[str, Any]) -> list[str]:
    """分镜配置校验（构建前置）。返回错误列表。"""
    errs = []
    segments = cfg.get("segments") or []
    if not segments:
        return ["没有分镜"]
    file_ids = {im.get("id") for im in (cfg.get("images") or []) if im.get("id")}
    for i, s in enumerate(segments):
        tag = "分镜 %d" % (i + 1)
        try:
            start, end = int(s["startFrame"]), int(s["endFrame"])
        except (KeyError, TypeError, ValueError):
            errs.append("%s：起止帧缺失或非法" % tag)
            continue
        if end <= start:
            errs.append("%s：结束帧(%d)必须大于起始帧(%d)"
                        % (tag, end, start))
        p = str(s.get("prompt") or "").strip()
        if not p:
            errs.append("%s：提示词为空" % tag)
        # inherit 链检查：本镜无图且上一镜也无图 → 断链（首帧素材可充当链起点）
        own = [x for x in (s.get("images") or []) if x in file_ids]
        ff = [str(x) for x in (s.get("firstFrame") or []) if str(x).strip()]
        if i == 0 and not own and not ff:
            errs.append("%s：首镜必须指定参考图或首帧素材（inherit 链的起点）" % tag)
        for vid in (s.get("refVideos") or []):
            if not str(vid).strip():
                errs.append("%s：存在空的参考视频项" % tag)
        for aud in (s.get("refAudios") or []):
            if not str(aud).strip():
                errs.append("%s：存在空的参考音频项" % tag)
        # 首帧 / 尾帧素材：独立字段，仅接受图片类型素材
        for fld, label in (("firstFrame", "首帧素材"), ("lastFrame", "尾帧素材")):
            items = [str(x) for x in (s.get(fld) or []) if str(x).strip()]
            if len(items) > MAX_FL:
                errs.append("%s：%s最多 1 个" % (tag, label))
            for x in items:
                if os.path.splitext(x)[1].lower() not in _IMG_EXT:
                    errs.append("%s：%s必须是图片文件（%s）" % (tag, label, x))
    if (cfg.get("secondPass") and not str(cfg.get("secondPassModel") or "").strip()):
        errs.append("二采已开启但未选择 latent 放大模型（SelfLift 需要）")
    return errs


# ---------------------------------------------------------------- 导出
def build_timeline_data(cfg: dict[str, Any],
                        segments: list[dict[str, Any]],
                        t: dict[str, Any], width: int, height: int,
                        fps: float) -> dict[str, Any]:
    """组装 timeline_data v4（AIMixer 兼容结构，仅供 Director 消费）。

    画布 / fps / 段间引导 / 全局兜底在此；各镜 prompt 与参考素材
    以外部队（r2v 组）为准，这里只保留段元信息。
    """
    ovls = t["ovls"]
    # 段间引导（自动首尾帧续写）：前端开关为权威（cfg["continuity"]）；
    # 缺省（旧存档 / 直接调 API）时沿用「相邻段是否有重叠帧」的判定
    cont_sw = cfg.get("continuity")
    cont_on = bool(t["cont_on"] and
                   (t["cont_on"] if cont_sw is None else bool(cont_sw)))
    cont_frames = t["cont_frames"] if cont_on else 0
    ref_max = int(cfg.get("ref_max_size", 864) or 864)
    return {
        "version": 4,
        "editMode": "segment",
        "timelineMode": "prompt_batch",
        "totalFrames": t["total_frames"],
        "frameRate": float(fps),
        "width": width,
        "height": height,
        "refMaxSize": ref_max,
        "output": {
            "mode": "fixed",
            "longEdge": ref_max,
            "width": width,
            "height": height,
            "maxExportFrames": 0,
            "exportMode": "all",
            "audioMode": "generate",
            "continuityEnabled": cont_on,
            "continuityOverlapFrames": cont_frames,
        },
        "videoClips": [],
        "video": {"fileName": "", "videoFile": "", "subfolder": "",
                  "type": "input", "frames": [], "frameMap": []},
        # commonEnabled=True：Director 在 r2v 外接组模式下据此把全局提示词
        # 拼到每段提示词开头（concat_common_segment_prompt：全局 + 空行 + 本段）；
        # 为 False 时全局提示词只作「本段提示词为空」的兜底，不会生效。
        # refs / refAudios 留空，故开启 common 不会额外并入公共参考素材
        "global": {"taskType": "r2v",
                   "prompt": cfg.get("globalPrompt", ""),
                   "refs": [], "referenceVideo": {},
                   "continuousReference": False, "commonEnabled": True},
        "segments": [
            {"id": "s%d" % i,
             "start": int(s["startFrame"]),
             "length": int(s["endFrame"]) - int(s["startFrame"]),
             "prompt": str(s.get("prompt") or ""),
             "taskType": "",
             "refs": [],
             "referenceVideo": {},
             # 段 0 恒 false；续写关闭时所有段均 false（各段独立生成）
             "continuityFromPrev": bool(cont_on and ovls[i] >= 1)}
            for i, s in enumerate(segments)
        ],
    }


def timeline_json(**kw) -> str:
    """build_timeline_data 的 JSON 字符串版（直接塞进 Director 输入）。"""
    return json.dumps(build_timeline_data(**kw), ensure_ascii=False)


# ---------------------------------------------------------------- 提示词引用
# 提示词内嵌媒体引用：@素材:文件名 —— 空白、@、中英文常用标点结束
# （ASCII 的 () : 保留在文件名字符内，兼容 img(1).png 与 D:/path 盘符路径）
PROMPT_REF_RE = re.compile(
    r"@素材:([^\s@，。；、,;：！？（）【】《》「」『』“”‘’…!?\"']+)")
_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_VID_EXT = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".ts"}
_AUD_EXT = {".wav", ".mp3", ".flac", ".ogg", ".aac", ".m4a", ".opus"}
_FIELD_CAP = {"refVideos": MAX_REF["videos"],
              "refAudios": MAX_REF["audios"]}   # images 不设上限（无 cap 键）


def _ref_field(name: str) -> str | None:
    """按扩展名归属引用字段。"""
    ext = os.path.splitext(name)[1].lower()
    if ext in _IMG_EXT:
        return "images"
    if ext in _VID_EXT:
        return "refVideos"
    if ext in _AUD_EXT:
        return "refAudios"
    return None


# 已知媒体扩展名（用于截断被贪婪匹配吞入的正文，如 photo(1).png继续）
_MEDIA_EXT_RE = re.compile(
    r"\.(png|jpe?g|webp|gif|bmp|mp4|webm|mkv|mov|avi|m4v|ts|"
    r"wav|mp3|flac|ogg|aac|m4a|opus)(?![a-zA-Z0-9._-])", re.I)


def _trim_ref_name(name: str) -> str:
    """在首个合法媒体扩展名边界处截断记号名。

    文件名段允许 ASCII 的 () : 等（兼容 img(1).png、D:/path），但也可能把
    紧随其后的正文一并吞入 —— 若整体不是已知扩展名结尾，则按扩展名边界裁掉。
    """
    if os.path.splitext(name)[1].lower() in (_IMG_EXT | _VID_EXT | _AUD_EXT):
        return name
    m = _MEDIA_EXT_RE.search(name)
    return name[:m.end()] if m else name


def _resolve_ref_name(name: str) -> str | None:
    """记号名 → 素材库 key：库内已有 / 取 basename / 本地路径导入。"""
    from xqwui.storage import router as storage
    for cand in (name, name.replace("\\", "/").split("/")[-1]):
        if cand and storage.exists("materials", cand):
            return cand
    path = os.path.expanduser(name)
    if os.path.isfile(path):
        key = os.path.basename(path)
        try:
            storage.put_file("materials", key, path)
            return key
        except storage.StorageError:
            return None
    return None


def resolve_prompt_refs(cfg: dict[str, Any]) -> dict[str, Any]:
    """解析各镜提示词中的 @素材: 引用记号（提示词引用媒体）。

    · 记号解析到素材库文件 → 按类型并入本镜 images/refVideos/refAudios
      （去重、遵守上限；图片自然进入 inherit 链），记号从提示词移除
    · 记号为已存在的本地路径 → 先导入素材库再并入
    · 无法解析的记号保留原文（视为普通提示词文本）
    返回新 cfg（深拷贝），不修改入参；无记号时原对象返回。
    """
    segments = cfg.get("segments") or []
    if not any(PROMPT_REF_RE.search(str(s.get("prompt") or ""))
               for s in segments):
        return cfg
    cfg = copy.deepcopy(cfg)
    if not isinstance(cfg.get("images"), list):
        cfg["images"] = []
    known_ids = {im.get("id") for im in cfg["images"]}
    for s in cfg["segments"]:
        prompt = str(s.get("prompt") or "")
        if not prompt:
            continue
        resolved: dict[str, str] = {}

        def _sub(m: re.Match[str]) -> str:
            name = m.group(1)
            trimmed = _trim_ref_name(name)
            tail = name[len(trimmed):]        # 截断掉的正文原样保留
            key = _resolve_ref_name(trimmed)
            field = _ref_field(key or trimmed)
            if key and field:
                resolved[key] = field
                return tail                   # 解析成功 → 移除记号（留正文）
            return m.group(0)                 # 保留原文

        new_prompt = PROMPT_REF_RE.sub(_sub, prompt)
        if not resolved:
            continue
        s["prompt"] = re.sub(r"[ \t\u3000]{2,}", " ", new_prompt).strip()
        # 记号原本夹在句中时（如「A，@素材:x.png，B」），移除后会留下
        # 悬空双逗号 —— 折叠为单个逗号（仅相邻逗号，不影响正常文本）
        s["prompt"] = re.sub(r"[，,]\s*[，,]", "，", s["prompt"])
        for key, field in resolved.items():
            lst = [x for x in (s.get(field) or []) if x]
            if key not in lst:
                lst.append(key)
            cap = _FIELD_CAP.get(field)
            s[field] = lst[:cap] if cap else lst
            if field == "images" and key not in known_ids:
                known_ids.add(key)
                cfg["images"].append({"id": key, "file": key})
    return cfg
