"""system —— 系统状态 / 健康 / 设置 / 模型清单 / 引擎控制与测试。

  GET  /api/system/status   应用+引擎+存储综合状态
  GET  /api/system/health   存活探针
  GET  /api/system/about    关于系统（?check=1 附带在线检查更新）
  GET  /api/settings        读取配置
  POST /api/settings        更新配置（engine_url / token / 云端引擎等）
  GET  /api/settings/snapshots       配置快照清单
  POST /api/settings/snapshots/rollback   回滚到指定快照 {file}
  GET  /api/logs            日志文件清单 + 占用 + 切割配置
  GET  /api/logs/content    查看日志尾部 {file?, lines?}（支持 .gz）
  POST /api/logs/rotate     手动触发一次切割
  POST /api/logs/prune      立即按保留策略清理历史文件
  POST /api/logs/clear      清空历史文件 {include_current?}
  POST /api/engine/test     引擎连通性测试 {url?} → 版本 / 延迟 / 错误
  GET  /api/models          引擎模型清单（全类别）
  POST /api/engine/free     释放引擎模型与显存
  GET  /api/engine/history  引擎近期执行历史（状态排查用）
"""
import json
import logging
import platform
import time
import urllib.error
import urllib.request

from xqwui import config as appcfg
from xqwui import logutil
from xqwui.engine_client import EngineClient, EngineError
from xqwui.scheduler import get_manager
from xqwui.storage import router as storage

LOG = logging.getLogger("xqwui.api.system")

_STARTED = time.time()

# 关于系统：应用身份信息（版本升级时同步更新）
APP_NAME = "XQWUI 漫剧工作台"
APP_SUBTITLE = "XQWUI · 独立 AI 视频生成工作台"
APP_VERSION = "0.0.1"
APP_BUILD = "2026-09-27"
APP_LICENSE = "CC BY-NC-SA 4.0"
APP_LICENSE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
APP_COPYRIGHT = "© 2026 comfy-小青蛙UI"

MODEL_KINDS = ("diffusion_models", "text_encoders", "vae", "loras",
               "latent_upscale_models")

# 采样参数真实来源：引擎 /object_info 的节点 COMBO 输入。
# 本引擎用 MiniMaxH3Director；标准 ComfyUI 后端探测 KSampler。
_OPT_NODES = (("MiniMaxH3Director", "sampler", "scheduler"),
              ("KSampler", "sampler_name", "scheduler"))


def _combo_of(info: dict, *names: str) -> list:
    """从节点 object_info 里取指定输入的 COMBO 选项列表。"""
    inputs = info.get("input") or {}
    for n in names:
        spec = (inputs.get("required") or {}).get(n) \
            or (inputs.get("optional") or {}).get(n)
        if isinstance(spec, (list, tuple)) and spec \
                and isinstance(spec[0], (list, tuple)):
            vals = [str(x) for x in spec[0]]
            if vals:
                return vals
    return []


def _model_names(r) -> list:
    """归一化引擎 /models/<kind> 响应为文件名列表。
    兼容：纯数组（标准 ComfyUI）、{"models": [...]}、{名称: 路径} 字典。"""
    if isinstance(r, list):
        return [str(x) for x in r]
    if isinstance(r, dict):
        m = r.get("models")
        if isinstance(m, list):
            return [str(x) for x in m]
        return [str(x) for x in r.keys()]
    return []


def _engine_options(eng: EngineClient) -> dict:
    """采样器 / 调度器真实选项（引擎不可达或节点缺失时返回空列表）。"""
    for node_name, skey, sched_key in _OPT_NODES:
        try:
            r = eng._http("GET", "/object_info/%s" % node_name, timeout=15)
        except EngineError:
            continue
        info = r.get(node_name) if isinstance(r, dict) else None
        if not isinstance(info, dict):
            continue
        samplers = _combo_of(info, skey)
        schedulers = _combo_of(info, sched_key)
        if samplers and schedulers:
            return {"samplers": samplers, "schedulers": schedulers}
    return {"samplers": [], "schedulers": []}


def _log_settings(body: dict) -> dict:
    """归一化并校验日志设置项；非法值抛 ValueError / TypeError。"""
    out = {}
    if "log_rotate_max_mb" in body:
        out["log_rotate_max_mb"] = min(max(0.0, float(body["log_rotate_max_mb"])),
                                       4096.0)
    if "log_rotate_when" in body:
        v = str(body["log_rotate_when"] or "").strip().lower()
        if v not in logutil.WHENS:
            raise ValueError("log_rotate_when 必须是 off / hourly / midnight")
        out["log_rotate_when"] = v
    if "log_keep_files" in body:
        out["log_keep_files"] = min(max(0, int(body["log_keep_files"])), 500)
    if "log_max_total_mb" in body:
        out["log_max_total_mb"] = min(
            max(0.0, float(body["log_max_total_mb"])), 102400.0)
    if "log_archive_gzip" in body:
        out["log_archive_gzip"] = 1 if body["log_archive_gzip"] else 0
    if "log_rotate_marker" in body:
        out["log_rotate_marker"] = 1 if body["log_rotate_marker"] else 0
    if "log_level" in body:
        v = str(body["log_level"] or "").strip().upper()
        if v not in logutil.LEVELS:
            raise ValueError("log_level 必须是 DEBUG / INFO / WARNING / ERROR")
        out["log_level"] = v
    return out


def register(app):
    @app.get("/api/system/status")
    def status(ctx, q, body):
        mgr = get_manager()
        ctx.json({
            "app": {"version": APP_VERSION,
                    "uptime_s": int(time.time() - _STARTED),
                    "data_dir": appcfg.DATA_DIR},
            "engine": mgr.engine_status(),
            "storage": storage.health(),
        })

    @app.get("/api/system/about")
    def about(ctx, q, body):
        """关于系统：名称 / 版本 / 构建 / 许可；check=1 时在线检查更新。

        更新源经 config.json 的 update_url 配置（返回 {version, notes}
        的 JSON 端点）；未配置时不做任何外网请求。homepage 为官网 /
        支持页地址（可选）。"""
        cfg = appcfg.load()
        out: dict = {
            "name": APP_NAME, "subtitle": APP_SUBTITLE,
            "version": APP_VERSION, "build_date": APP_BUILD,
            "python": platform.python_version(),
            "license": APP_LICENSE, "license_url": APP_LICENSE_URL,
            "copyright": APP_COPYRIGHT,
            "update_url": str(cfg.get("update_url") or "").strip(),
            "homepage": str(cfg.get("homepage") or "").strip(),
            "data_dir": appcfg.DATA_DIR,
        }
        if (q.get("check") or "") == "1":
            url = out["update_url"]
            if not url:
                out["update"] = {"ok": False,
                                 "reason": "未配置更新源（config.json → update_url）"}
            else:
                try:
                    req = urllib.request.Request(
                        url, headers={"User-Agent": "XQWUI/" + APP_VERSION})
                    with urllib.request.urlopen(req, timeout=6) as r:
                        data = json.loads(r.read().decode("utf-8") or "{}")
                    latest = str(data.get("version") or "").lstrip("vV ")
                    out["update"] = {
                        "ok": True, "latest": latest,
                        "up_to_date": not latest or latest <= APP_VERSION,
                        "notes": str(data.get("notes")
                                     or data.get("body") or "")[:2000]}
                except (urllib.error.URLError, OSError, ValueError) as e:
                    out["update"] = {"ok": False, "reason": str(e)[:200]}
        ctx.json(out)

    @app.get("/api/system/health")
    def health(ctx, q, body):
        ctx.json({"ok": True, "ts": time.time()})

    @app.get("/api/settings")
    def get_settings(ctx, q, body):
        cfg = dict(appcfg.load())
        ctx.json({"settings": cfg, "web_dir": appcfg.WEB_DIR})

    @app.post("/api/settings")
    def set_settings(ctx, q, body):
        if not isinstance(body, dict):
            ctx.error("请求体必须是 JSON 对象")
        allowed = ("engine_url", "token", "host", "port", "engine_retry",
                  "cloud_engine_url", "cloud_engine_token",
                  "queue_limit", "disk_reserve_gb", "update_url", "homepage",
                  "log_rotate_max_mb", "log_rotate_when", "log_keep_files",
                  "log_max_total_mb", "log_archive_gzip",
                  "log_rotate_marker", "log_level")
        update = {k: body[k] for k in allowed if k in body}
        if "port" in update:
            try:
                update["port"] = int(update["port"])
            except (TypeError, ValueError):
                ctx.error("port 必须是整数")
        if "engine_retry" in update:
            try:
                update["engine_retry"] = max(1, int(update["engine_retry"]))
            except (TypeError, ValueError):
                ctx.error("engine_retry 必须是整数")
        if "queue_limit" in update:
            try:
                update["queue_limit"] = max(0, int(update["queue_limit"]))
            except (TypeError, ValueError):
                ctx.error("queue_limit 必须是非负整数（0=不限）")
        if "disk_reserve_gb" in update:
            try:
                update["disk_reserve_gb"] = max(0.0,
                                                float(update["disk_reserve_gb"]))
            except (TypeError, ValueError):
                ctx.error("disk_reserve_gb 必须是非负数字（0=不检）")
        if any(k.startswith("log_") for k in body):
            try:
                update.update(_log_settings(body))
            except (TypeError, ValueError):
                ctx.error("日志设置非法：大小/份数需为数字，策略为 "
                          "off/hourly/midnight，级别为 DEBUG/INFO/WARNING/ERROR")
        # 先把旧配置存为快照（回滚用），再保存新配置
        try:
            appcfg.snapshot_config()
        except OSError:
            LOG.warning("配置快照失败（不影响本次保存）", exc_info=True)
        cfg = appcfg.save(update)
        if any(k.startswith("log_") for k in update):
            # 切割策略/级别热更新：无需重启，立即生效
            try:
                logutil.reconfigure()
            except Exception:
                LOG.warning("日志配置热更新失败（重启后生效）", exc_info=True)
        ctx.json({"settings": cfg})

    # ------------------------------------------------------------ 日志管理
    @app.get("/api/logs")
    def logs_list(ctx, q, body):
        ctx.json(logutil.list_files())

    @app.get("/api/logs/content")
    def logs_content(ctx, q, body):
        file = str(q.get("file") or "") or logutil.CURRENT_NAME
        try:
            lines = int(q.get("lines") or 200)
        except (TypeError, ValueError):
            lines = 200
        try:
            ctx.json(logutil.read_tail(file, lines))
        except OSError as e:
            ctx.error(str(e), 404)

    @app.post("/api/logs/rotate")
    def logs_rotate(ctx, q, body):
        name = logutil.rotate_now("manual")
        LOG.info("op=log-rotate 触发=manual 文件=%s", name or "-")
        ctx.json({"ok": bool(name), "file": name,
                  "files": logutil.list_files()})

    @app.post("/api/logs/prune")
    def logs_prune(ctx, q, body):
        r = logutil.prune_now()
        LOG.info("op=log-prune 删除=%d 释放=%d", len(r["removed"]), r["freed"])
        ctx.json({"ok": True, "removed": r["removed"], "freed": r["freed"],
                  "files": logutil.list_files()})

    @app.post("/api/logs/clear")
    def logs_clear(ctx, q, body):
        inc = bool((body or {}).get("include_current"))
        if inc and not (body or {}).get("confirm"):
            ctx.error("清空当前日志需带 confirm=true")
        r = logutil.clear(include_current=inc)
        LOG.warning("op=log-clear 删除=%d 含当前=%s", len(r["removed"]), inc)
        ctx.json({"ok": True, "removed": r["removed"], "freed": r["freed"],
                  "files": logutil.list_files()})

    @app.get("/api/settings/snapshots")
    def list_snapshots(ctx, q, body):
        ctx.json({"snapshots": appcfg.list_snapshots(),
                  "keep": appcfg.SNAPSHOT_KEEP})

    @app.post("/api/settings/snapshots/rollback")
    def rollback_snapshot(ctx, q, body):
        fn = str((body or {}).get("file") or "")
        if not fn:
            ctx.error("缺少快照文件名 file")
        try:
            cfg = appcfg.rollback_snapshot(fn)
        except OSError as e:
            ctx.error(str(e), 404)
            return
        LOG.warning("op=settings-rollback file=%s", fn)
        ctx.json({"settings": cfg})

    @app.post("/api/engine/test")
    def engine_test(ctx, q, body):
        """引擎连通性测试：返回版本 / GPU / 延迟；失败给明确错误。"""
        url = str((body or {}).get("url") or "").strip() \
            or str(appcfg.load().get("engine_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            ctx.json({"ok": False, "url": url,
                      "error": "地址必须以 http:// 或 https:// 开头"})
            return
        t0 = time.time()
        try:
            stats = EngineClient(url).system_stats()
        except EngineError as e:
            ctx.json({"ok": False, "url": url, "error": str(e),
                      "latency_ms": int((time.time() - t0) * 1000)})
            return
        dev = (stats.get("devices") or [{}])[0]
        ctx.json({
            "ok": True, "url": url,
            "version": (stats.get("system") or {}).get("comfyui_version", ""),
            "gpu": dev.get("name", ""),
            "latency_ms": int((time.time() - t0) * 1000),
        })

    @app.get("/api/models")
    def models(ctx, q, body):
        """模型清单代理聚合；kind 可用 query 过滤；src=cloud 时走云端引擎地址；
        url 显式指定引擎地址（测试连接成功后按该地址加载真实清单，优先生效）。"""
        only = q.get("kind") or ""
        kinds = [k for k in MODEL_KINDS if not only or k == only]
        url = str(q.get("url") or "").strip()
        if url:
            if not url.startswith(("http://", "https://")):
                ctx.error("地址必须以 http:// 或 https:// 开头", 400)
                return
            eng = EngineClient(url)
        elif (q.get("src") or "") == "cloud":
            url = str(appcfg.load().get("cloud_engine_url") or "").strip()
            if not url:
                ctx.error("云端引擎地址未配置", 400)
                return
            eng = EngineClient(url)
        else:
            eng = get_manager().engine()
        out = {}
        fail = 0
        for k in kinds:
            try:
                r = eng._http("GET", "/models/%s" % k, timeout=60)
            except EngineError:
                fail += 1
                out[k] = []
                continue
            out[k] = _model_names(r)
        if fail >= len(kinds):
            ctx.error("获取模型清单失败: 引擎各类别接口均不可用", 502)
            return
        ctx.json({"models": out, "options": _engine_options(eng)})

    @app.post("/api/engine/free")
    def engine_free(ctx, q, body):
        try:
            r = get_manager().engine().free(
                unload_models=bool((body or {}).get("unload_models", True)),
                free_memory=bool((body or {}).get("free_memory", True)))
        except EngineError as e:
            ctx.error(str(e), 502)
        ctx.json({"ok": True, "engine": r})

    @app.get("/api/engine/history")
    def engine_history(ctx, q, body):
        try:
            r = get_manager().engine().history()
        except EngineError as e:
            ctx.error(str(e), 502)
        items = []
        for pid, entry in (r or {}).items():
            items.append({
                "prompt_id": pid,
                "status": (entry.get("status") or {}),
                "outputs": entry.get("outputs") or {},
            })
        ctx.json({"history": items[-int(q.get("limit") or 50):]})
