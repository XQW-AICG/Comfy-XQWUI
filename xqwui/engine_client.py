"""engine_client —— 推理引擎客户端（Comfy 后端 API 规范）。

HTTP：提交工作流 / 队列与中断 / 历史 / 上传素材 / 下载产物
WS  ：EngineWatcher 订阅引擎 /ws 事件（断线自动重连），供调度器跟踪执行。
"""
import json
import os
import threading
import urllib.error
import urllib.request
import uuid

from shared.ws import WSClient, WSError


class EngineError(Exception):
    """引擎调用失败（HTTP 错误 / 不可达）。

    kind 标识错误类别（任务 error_kind 落盘 / 前端徽标 / 重试决策用）：
      engine_4xx        请求或工作流校验问题（改配置再试，重试无益）
      engine_5xx        引擎内部错误（可原样重试）
      engine_unreachable 引擎离线 / 地址不可达
      network           连接层失败（超时 / 重置）
    """

    def __init__(self, message: str, kind: str = "engine_5xx"):
        super().__init__(message)
        self.kind = kind


def ws_url_of(base_url: str, client_id: str) -> str:
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[8:] + "/ws?clientId=" + client_id
    return "ws://" + base[7:] + "/ws?clientId=" + client_id


class EngineClient:
    """无状态 HTTP 客户端（base_url 可动态更新）。"""

    def __init__(self, base_url: str):
        self.base = (base_url or "").rstrip("/")

    # ------------------------------------------------------------ 基础
    def _http(self, method: str, path: str, body=None, timeout: int = 30,
              content_type: str = ""):
        data = None
        headers = {}
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
                headers["Content-Type"] = content_type or \
                    "application/octet-stream"
            else:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
        except urllib.error.HTTPError as e:
            kind = "engine_4xx" if 400 <= e.code < 500 else "engine_5xx"
            try:
                msg = json.loads(e.read().decode("utf-8"))
                detail = msg.get("error") or msg
                # 保留 node_errors（具体哪个节点 / 哪个输入校验失败），便于定位
                nodes = msg.get("node_errors") or {}
                if nodes:
                    detail = "%s | 节点校验详情: %s" % (
                        detail, json.dumps(nodes, ensure_ascii=False))
            except Exception:
                detail = str(e)
            raise EngineError("引擎返回 %s: %s"
                              % (e.code, str(detail)[:1600]), kind) from None
        except urllib.error.URLError as e:
            raise EngineError("引擎不可达（%s）" % (e.reason or e),
                              "engine_unreachable") from None
        except OSError as e:
            raise EngineError("引擎连接失败: %s" % e, "network") from None
        if not payload:
            return {}
        try:
            return json.loads(payload.decode("utf-8"))
        except ValueError:
            return payload

    @staticmethod
    def _multipart(field: str, filename: str, data: bytes):
        """构造 multipart 请求体（规范格式）。返回 (body_bytes, boundary_str)。"""
        b = "----xqwui" + uuid.uuid4().hex
        head = ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; "
                "filename=\"%s\"\r\nContent-Type: application/octet-stream"
                "\r\n\r\n" % (b, field, filename)).encode("utf-8")
        tail = ("\r\n--%s--\r\n" % b).encode("utf-8")
        return head + data + tail, b

    # ---- Comfy 规范接口
    def system_stats(self) -> dict:
        return self._http("GET", "/system_stats", timeout=10)

    def object_info(self, node_class: str) -> dict:
        """查询单个节点类型的输入定义（不存在时返回空 dict）。"""
        try:
            return self._http("GET", "/object_info/" + node_class, timeout=15)
        except EngineError:
            return {}

    def queue(self) -> dict:
        return self._http("GET", "/queue", timeout=10)

    def post_prompt(self, graph: dict, client_id: str) -> dict:
        """提交工作流；node_errors 时抛 EngineError（附详情）。"""
        r = self._http("POST", "/prompt",
                       {"prompt": graph, "client_id": client_id})
        if r.get("node_errors"):
            raise EngineError("工作流校验失败: %s"
                              % json.dumps(r["node_errors"],
                                           ensure_ascii=False)[:2000],
                              "engine_4xx")
        if not r.get("prompt_id"):
            raise EngineError("引擎未返回 prompt_id: %s" % r)
        return r

    def interrupt(self, prompt_id: str | None = None):
        body = {"prompt_id": prompt_id} if prompt_id else {}
        return self._http("POST", "/interrupt", body)

    def free(self, unload_models: bool = True, free_memory: bool = True):
        return self._http("POST", "/free", {
            "unload_models": unload_models, "free_memory": free_memory})

    def history(self, prompt_id: str | None = None) -> dict:
        return self._http("GET", "/history/%s" % prompt_id
                          if prompt_id else "/history", timeout=15)

    def upload(self, filename: str, data: bytes,
               overwrite: bool = True) -> str:
        """上传素材到引擎输入目录，返回引擎侧文件名。"""
        body, boundary = self._multipart("image", filename, data)
        r = self._http("POST", "/upload/image?overwrite=%s"
                       % ("true" if overwrite else "false"),
                       body=body, timeout=120,
                       content_type="multipart/form-data; boundary=%s"
                       % boundary)
        return r.get("name") or filename

    def upload_from_file(self, filename: str, path: str,
                         overwrite: bool = True) -> str:
        """流式上传本地文件到引擎输入目录（1MB 分块，常量内存）。

        与 upload(bytes) 的「整文件 + multipart 拼接」两份拷贝不同，
        本方法按块直发，峰值内存恒定 —— 大视频素材同步必用。"""
        import http.client
        from urllib.parse import urlsplit
        boundary = "----xqwui" + uuid.uuid4().hex
        head = (
            "--%s\r\nContent-Disposition: form-data; name=\"image\"; "
            "filename=\"%s\"\r\nContent-Type: application/octet-stream"
            "\r\n\r\n" % (boundary, filename)).encode("utf-8")
        tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        u = urlsplit(self.base)
        cls = (http.client.HTTPSConnection if u.scheme == "https"
               else http.client.HTTPConnection)
        conn = cls(u.netloc, timeout=600)
        resp = None
        try:
            conn.putrequest("POST", "/upload/image?overwrite=%s"
                            % ("true" if overwrite else "false"),
                            skip_accept_encoding=True)
            conn.putheader("Content-Type",
                           "multipart/form-data; boundary=%s" % boundary)
            conn.putheader("Content-Length",
                           str(len(head) + os.path.getsize(path) + len(tail)))
            conn.endheaders()
            conn.send(head)
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    conn.send(chunk)
            conn.send(tail)
            resp = conn.getresponse()
            payload = resp.read()
        finally:
            conn.close()
        if resp is None or resp.status != 200:
            raise EngineError("上传素材失败: %s"
                              % (payload[:200] if resp else "无响应"))
        try:
            return json.loads(payload.decode("utf-8")).get("name") or filename
        except ValueError:
            return filename

    def download(self, filename: str, subfolder: str = "",
                 ftype: str = "output", dst: str | None = None):
        """下载引擎产物。dst 为空返回 bytes，否则写文件返回 dst。"""
        from urllib.parse import quote
        path = ("/view?type=%s&subfolder=%s&filename=%s"
                % (quote(ftype), quote(subfolder), quote(filename)))
        payload = self._http("GET", path, timeout=600)
        if isinstance(payload, dict):        # 引擎返回了 JSON 错误
            raise EngineError("下载产物失败: %s" % payload)
        if dst:
            with open(dst, "wb") as f:
                f.write(payload)
            return dst
        return payload


class EngineWatcher(threading.Thread):
    """订阅引擎 WS 事件，断线自动重连；事件回调在 watcher 线程执行。"""

    def __init__(self, get_base, on_event, client_id: str | None = None,
                 retry: float = 3.0):
        super().__init__(daemon=True, name="engine-watcher")
        self._get_base = get_base        # () -> 引擎 http base（跟随配置变化）
        self._on_event = on_event
        self._cid = client_id or ("xqwui-" + uuid.uuid4().hex[:8])
        self._retry = retry
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            client = None
            try:
                client = WSClient(ws_url_of(self._get_base(), self._cid))
                client.connect()
                self._emit({"type": "engine_status",
                            "data": {"connected": True}})
                while not self._stop.is_set():
                    kind, *rest = client.recv(timeout=1.0)
                    if kind == "timeout":
                        continue
                    if kind == "text" and rest:
                        self._emit(rest[0])
                    # 二进制预览帧：v1 不向工作台转发
            except (WSError, OSError, ValueError):
                pass
            finally:
                if client:
                    client.close()
            if not self._stop.is_set():
                self._emit({"type": "engine_status",
                            "data": {"connected": False}})
                self._stop.wait(self._retry)

    def _emit(self, msg: dict):
        try:
            self._on_event(msg)
        except Exception:
            pass

    def stop(self):
        self._stop.set()
