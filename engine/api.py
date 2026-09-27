"""api —— 引擎 HTTP/WS 层（Comfy 后端 API 规范实现）。

路由与消息格式基于 ComfyUI v0.37.2 提取的规范：
  REST   /prompt /queue /interrupt /free /history /object_info
         /system_stats /upload/image /view
  WS     status / execution_start / executing / progress / executed /
         execution_cached / execution_success / execution_error /
         execution_interrupted；二进制预览 = 4字节大端事件类型(8) + JPEG
"""
import json
import os
import re
import time
import uuid

from shared.httpd import App, HttpError
from shared.util import safe_name
from shared.ws import WSHub

from engine import config as cfg
from engine import nodes as _nodes  # noqa: F401 导入即注册
from engine.executor import GraphExecutor, validate_prompt
from engine.compute import list_backends
from engine.compute.loader import get_backend, ComputeBackendError
from engine.nodes.loaders import scan_models
from engine.queueman import QueueManager

PREVIEW_EVENT = 8            # Comfy 规范：PREVIEW_IMAGE


class EngineApp(App):
    def __init__(self):
        super().__init__(static_dir=None)
        cfg.ensure_dirs()
        self.hub = WSHub()
        self.queue = QueueManager(history_max=cfg.get_cfg()["history_max"])
        self.executor = GraphExecutor(
            self.queue,
            on_event=self._on_exec_event)
        self._clients: dict = {}          # prompt_id -> client_id
        self._client_sids: dict = {}      # client_id -> ws sid
        self._routes()

    # ------------------------------------------------ 事件 → WS

    def _on_exec_event(self, event: str, data: dict):
        if event == "_binary":
            self.hub.broadcast_binary(data["event"], data["data"],
                                      sid=data.get("sid"))
            return
        if event == "status":
            self._broadcast_status()
            return
        payload = {k: v for k, v in data.items() if not k.startswith("_")}
        client_id = self._clients.get(payload.get("prompt_id"))
        sid = self._client_sids.get(client_id)
        self.hub.broadcast_json(event, payload, sid=sid)

    def _broadcast_status(self):
        q = self.queue.get_queue()
        self.hub.broadcast_json("status", {
            "status": {"exec_info": {
                "queue_remaining": len(q["queue_pending"]) +
                (1 if q["queue_running"] else 0)}}})

    def _routes(self):
        # ---------------- WebSocket
        @self.on_websocket
        def ws_handler(conn, q):
            client_id = str(q.get("clientId") or "")
            self.hub.add(conn)
            if client_id:
                self._client_sids[client_id] = conn.sid
            try:
                qq = self.queue.get_queue()
                conn.send_json("status", {
                    "status": {"exec_info": {
                        "queue_remaining": len(qq["queue_pending"]) +
                        (1 if qq["queue_running"] else 0)}},
                    "sid": conn.sid})
                while conn.alive:
                    msg = conn.recv_text(timeout=2.0)
                    if msg is None:
                        continue
                    try:
                        data = json.loads(msg)
                    except Exception:
                        continue
                    if data.get("type") == "feature_flags":
                        conn.send_json("feature_flags", {})
            finally:
                self.hub.remove(conn)
                if client_id and self._client_sids.get(client_id) == conn.sid:
                    self._client_sids.pop(client_id, None)

        # ---------------- 提交
        @self.post("/prompt")
        def post_prompt(ctx, q, body):
            prompt = body.get("prompt")
            if not isinstance(prompt, dict):
                return ctx.json({"error": {
                    "type": "no_prompt", "message": "No prompt provided",
                    "details": "No prompt provided", "extra_info": {}},
                    "node_errors": {}}, 400)
            prompt_id = body.get("prompt_id") or str(uuid.uuid4())
            if not re.match(r"^[0-9a-f-]{8,36}$", prompt_id):
                return ctx.json({"error": {
                    "type": "invalid_prompt_id",
                    "message": "prompt_id must be a valid UUID",
                    "details": "", "extra_info": {}},
                    "node_errors": {}}, 400)
            try:
                ok, error, outputs, node_errors = validate_prompt(
                    prompt_id, prompt)
            except Exception as e:
                return ctx.json({"error": {
                    "type": "prompt_validation_failed",
                    "message": str(e), "details": "", "extra_info": {}},
                    "node_errors": {}}, 400)
            if not ok:
                return ctx.json({"error": error,
                                 "node_errors": node_errors}, 400)
            client_id = str(body.get("client_id") or uuid.uuid4().hex)
            extra_data = dict(body.get("extra_data") or {})
            extra_data["client_id"] = client_id
            extra_data["create_time"] = int(time.time() * 1000)
            item = self.queue.put(prompt_id, prompt, extra_data, outputs)
            self._clients[prompt_id] = client_id
            self._broadcast_status()
            return ctx.json({"prompt_id": prompt_id,
                             "number": item[0], "node_errors": {}})

        # ---------------- 队列
        @self.get("/queue")
        def get_queue(ctx, q, body):
            qq = self.queue.get_queue()
            return ctx.json({
                "queue_running": [_slim(it) for it in qq["queue_running"]],
                "queue_pending": [_slim(it) for it in qq["queue_pending"]]})

        @self.post("/queue")
        def post_queue(ctx, q, body):
            if body.get("clear"):
                self.queue.clear_pending()
            if isinstance(body.get("delete"), list):
                self.queue.delete_pending(body["delete"])
            self._broadcast_status()
            return ctx.json({})

        # ---------------- 中断 / 释放
        @self.post("/interrupt")
        def post_interrupt(ctx, q, body):
            self.queue.request_interrupt(
                body.get("prompt_id") if isinstance(body, dict) else None)
            return ctx.json({})

        @self.post("/free")
        def post_free(ctx, q, body):
            try:
                get_backend().free(None)
            except ComputeBackendError:
                pass
            return ctx.json({})

        # ---------------- 历史
        @self.get("/history")
        def get_history(ctx, q, body):
            max_items = int(q.get("max_items") or 0) or None
            h = self.queue.get_history()
            if max_items:
                h = dict(list(h.items())[-max_items:])
            return ctx.json(h)

        @self.get("/history/*")
        def get_history_one(ctx, q, body):
            pid = ctx.h.path.rsplit("/", 1)[-1]
            return ctx.json(self.queue.get_history(pid))

        @self.post("/history")
        def post_history(ctx, q, body):
            if body.get("clear"):
                self.queue.clear_history()
            if isinstance(body.get("delete"), list):
                self.queue.delete_history(body["delete"])
            return ctx.json({})

        # ---------------- 元信息
        @self.get("/object_info")
        def get_object_info(ctx, q, body):
            from engine.registry import object_info
            return ctx.json(object_info())

        @self.get("/object_info/*")
        def get_object_info_one(ctx, q, body):
            name = ctx.h.path.rsplit("/", 1)[-1]
            from engine.registry import object_info
            oi = object_info()
            return ctx.json({name: oi[name]} if name in oi else {})

        @self.get("/system_stats")
        def system_stats(ctx, q, body):
            return ctx.json(self.stats())

        @self.get("/models/*")
        def get_models(ctx, q, body):
            kind = ctx.h.path.rsplit("/", 1)[-1]
            if kind not in ("diffusion_models", "text_encoders", "vae",
                            "loras", "latent_upscale_models"):
                return ctx.json({"error": "unknown model kind"}, 404)
            return ctx.json({"models": scan_models(kind)})

        # ---------------- 上传 / 下载
        @self.post("/upload/image")
        def upload_image(ctx, q, body):
            if not isinstance(body, dict) or "image" not in body:
                raise HttpError("缺少 image 字段（multipart）", 400)
            part = body["image"]
            name = part.get("filename") or "upload.png"
            safe = safe_name(os.path.basename(name),
                             cfg.get_cfg()["max_filename_len"]) or "upload"
            overwrite = str(q.get("overwrite") or "false").lower() == "true"
            d = cfg.input_dir()
            os.makedirs(d, exist_ok=True)
            fp = os.path.join(d, safe)
            if os.path.exists(fp) and not overwrite:
                stem, ext = os.path.splitext(safe)
                safe = "%s_%s%s" % (stem, uuid.uuid4().hex[:6], ext)
                fp = os.path.join(d, safe)
            with open(fp, "wb") as f:
                f.write(part["data"])
            return ctx.json({"name": safe, "subfolder": "",
                             "type": "input"})

        @self.get("/view")
        def get_view(ctx, q, body):
            ftype = q.get("type", "output")
            sub = q.get("subfolder", "")
            fn = q.get("filename", "")
            base = cfg.input_dir() if ftype == "input" else cfg.output_dir()
            root = os.path.realpath(base)
            full = os.path.realpath(os.path.join(root, sub, fn))
            if not full.startswith(root + os.sep) or \
                    not os.path.isfile(full):
                raise HttpError("文件不存在", 404)
            ctx.file(full, cache=True)

        # ---------------- 兼容：老客户端探活
        @self.get("/")
        def root(ctx, q, body):
            bks = list_backends()
            avail = {n: c.available() for n, c in bks.items()}
            return ctx.json({"server": "xqwui-engine", "version": "1.0",
                             "backends": avail})

    # ------------------------------------------------ 统计

    def stats(self) -> dict:
        import platform
        import shutil as _sh
        vm = {"os": platform.system() + " " + platform.release(),
              "ram_total": 0, "ram_free": 0}
        try:
            with open("/proc/meminfo") as f:
                info = {}
                for line in f:
                    k, _, v = line.partition(":")
                    info[k.strip()] = int(v.strip().split()[0]) * 1024
                vm["ram_total"] = info.get("MemTotal", 0)
                vm["ram_free"] = info.get("MemAvailable", 0)
        except Exception:
            pass
        dev = {"name": "cpu", "type": "cpu",
               "vram_total": 0, "vram_free": 0}
        try:
            r = subprocess_run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free",
                                "--format=csv,noheader,nounits"])
            if r.returncode == 0:
                name, tot, free = r.stdout.decode().strip().split(", ")[:3]
                dev = {"name": name.strip(), "type": "cuda",
                       "vram_total": int(tot) * 1024 * 1024,
                       "vram_free": int(free) * 1024 * 1024}
        except Exception:
            pass
        backend = None
        try:
            backend = get_backend().probe()
        except Exception as e:
            backend = {"error": str(e)}
        du = 0
        try:
            du = _sh.disk_usage(cfg.DATA_DIR).total
        except Exception:
            pass
        q = self.queue.get_queue()
        return {
            "system": {
                "os": vm["os"],
                "ram_total": vm["ram_total"],
                "ram_free": vm["ram_free"],
                "comfyui_version": "xqwui-engine/1.0",
                "python_version": platform.python_version(),
                "disk_total": du,
            },
            "devices": [dev],
            "compute_backend": backend,
            "queue": {
                "queue_running": 1 if q["queue_running"] else 0,
                "queue_pending": len(q["queue_pending"]),
            },
        }


def subprocess_run(args, timeout=10):
    import subprocess
    return subprocess.run(args, capture_output=True, timeout=timeout)


def _slim(item) -> list:
    """队列条目 → [number, prompt_id, prompt, extra_data, outputs]。"""
    number, prompt_id, prompt, extra_data, outputs = item
    return [number, prompt_id, prompt, extra_data, outputs]
