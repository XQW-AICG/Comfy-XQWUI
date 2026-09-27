"""httpd —— ThreadingHTTPServer 基座。

在 BaseHTTPRequestHandler 之上提供：
  · 路由表（精确路径 + 前缀路径）与统一分发
  · JSON 请求/响应工具
  · multipart/form-data 解析（文件上传）
  · Range 分片响应（视频拖动播放必需）
  · 静态目录服务（MIME 推断、no-store 控制）
  · 异常兜底（任何处理器异常都返回 JSON 500，不硬断连接）
"""
import inspect
import json
import mimetypes
import os
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

from shared.ws import WSError

MAX_BODY = 256 * 1024 * 1024        # 请求体上限（大图上传）


class HttpError(Exception):
    """业务异常：处理器里抛出即可变成对应状态码的 JSON 响应。"""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def parse_multipart(body: bytes, content_type: str) -> dict:
    """解析 multipart/form-data。

    返回 {name: {"filename": str|None, "data": bytes}}；重名字段取最后一个。
    """
    m = re.search(r'boundary=("?)([^";]+)\1', content_type or "")
    if not m:
        raise HttpError("multipart 缺少 boundary", 400)
    bnd = m.group(2).encode()
    start = body.find(b"--" + bnd)
    if start < 0:
        raise HttpError("multipart 无效：未找到边界", 400)
    # 以 CRLF+边界 作为分隔切分（二进制载荷安全，不受载荷内 CRLF 影响）；
    # 首边界已跳过，seg0 以其自带的尾部 CRLF 开头
    segs = body[start + 2 + len(bnd):].split(b"\r\n--" + bnd)
    out = {}
    for seg in segs:                     # seg0 即首个字段（带边界尾部 CRLF）
        if seg.startswith(b"--"):        # 结束标记 --boundary--
            break
        part = seg[2:] if seg.startswith(b"\r\n") else seg
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        name_m = re.search(r'name="([^"]*)"', disp)
        file_m = re.search(r'filename="([^"]*)"', disp)
        if name_m:
            out[name_m.group(1)] = {
                "filename": file_m.group(1) if file_m else None,
                "data": data}
    return out


def _handler_arity(fn) -> int:
    """处理器接受的位置参数个数（缓存到函数属性）。

    分发器按此自适应传参：声明 (ctx, q) 的处理器不再被强制要求
    第三参 body；钳制在 [2, 3] 区间。
    """
    cached = getattr(fn, "_arity", None)
    if cached:
        return cached
    try:
        n = sum(1 for p in inspect.signature(fn).parameters.values()
                if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                              inspect.Parameter.POSITIONAL_OR_KEYWORD))
    except (TypeError, ValueError):
        n = 3
    arity = max(2, min(3, n))
    try:
        fn._arity = arity
    except (AttributeError, TypeError):
        pass
    return arity


class App:
    """路由注册 + HTTP 服务器封装。

    路由处理器声明 (ctx, q) 或 (ctx, q, body) 均可（分发器自适应）——
    ctx 为 _Ctx 实例（响应工具），q 为解析后的查询参数 dict，
    body 为解析后的 JSON 或 multipart dict。
    """

    def __init__(self, static_dir: str | None = None):
        self.static_dir = static_dir
        self._exact = {}            # (method, path) -> (fn, arity)
        self._prefix = []           # [(method, prefix, (fn, arity))] 按注册顺序
        self._guards = []           # fn(ctx, q) -> bool（False 表示已响应）
        self._on_ws = None          # fn(conn, q) 处理 WebSocket 升级
        self._handler_cls = self._build_handler()

    # ---- 注册 API

    def route(self, method: str, path: str):
        def deco(fn):
            entry = (fn, _handler_arity(fn))
            if path.endswith("*"):
                self._prefix.append((method.upper(), path[:-1], entry))
            else:
                self._exact[(method.upper(), path)] = entry
            return fn
        return deco

    def get(self, path):
        return self.route("GET", path)

    def post(self, path):
        return self.route("POST", path)

    def delete(self, path):
        return self.route("DELETE", path)

    def guard(self, fn):
        self._guards.append(fn)
        return fn

    def on_websocket(self, fn):
        self._on_ws = fn
        return fn

    # ---- 内部

    def _build_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "XQWUI"

            def log_message(self, fmt, *args):    # 静默默认访问日志
                pass

            # ---- 响应工具

            def send_json(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type",
                                 "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def send_bytes(self, data: bytes, ctype: str, cache: bool = False):
                """发送内存中的字节内容（小文件场景，不做 Range）。"""
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control",
                                 "public, max-age=3600" if cache else "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def send_file(self, path: str, cache: bool = False,
                          download: str = ""):
                """发送本地文件（自动 Range / MIME）。
                download 非空时以附件形式下载（Content-Disposition）。"""
                ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                size = os.path.getsize(path)
                rng = self.headers.get("Range")
                start, end, code = 0, size - 1, 200
                extra = []
                if download:
                    extra.append(("Content-Disposition",
                                  'attachment; filename="%s"'
                                  % download.replace('"', "")))
                if rng:
                    m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
                    if m and (m.group(1) or m.group(2)):
                        if m.group(1):
                            start = int(m.group(1))
                            end = int(m.group(2)) if m.group(2) else size - 1
                        else:
                            start = max(0, size - int(m.group(2)))
                            end = size - 1
                        end = min(end, size - 1)
                        if start > end or start >= size:
                            self.send_response(416)
                            self.send_header("Content-Range", "bytes */%d" % size)
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        code = 206
                        extra.append(("Content-Range",
                                      "bytes %d-%d/%d" % (start, end, size)))
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header(
                    "Cache-Control",
                    "public, max-age=3600" if cache else "no-store, must-revalidate")
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                if self.command == "HEAD":
                    return
                with open(path, "rb") as f:
                    f.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk = f.read(min(1048576, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)

            # ---- 分发

            def _dispatch(self, method):
                u_path, _, u_query = self.path.partition("?")
                q = {k: v[0] for k, v in parse_qs(u_query or "").items()}
                for g in app._guards:
                    if not g(self, q):
                        return
                # WebSocket 升级
                if method == "GET" and \
                        (self.headers.get("Upgrade") or "").lower() == "websocket" \
                        and app._on_ws:
                    conn = self._ws_handshake()
                    self.close_connection = True   # WS 后不再复用 keep-alive
                    try:
                        app._on_ws(conn, q)
                    except (BrokenPipeError, ConnectionResetError,
                            OSError, WSError):
                        pass                       # 客户端断开属正常结束
                    except Exception:
                        traceback.print_exc()
                    finally:
                        conn.close()
                    return
                entry = app._exact.get((method, u_path))
                if entry is None:
                    for m, pre, e in app._prefix:
                        if m == method and u_path.startswith(pre):
                            entry = e
                            break
                if entry is None:
                    if app.static_dir and method == "GET" and \
                            self._try_static(u_path):
                        return
                    return self.send_json({"error": "unknown route"}, 404)
                fn, arity = entry
                body = self._read_body(method)
                try:
                    if arity >= 3:
                        fn(_Ctx(self), q, body)
                    else:
                        fn(_Ctx(self), q)
                except HttpError as e:
                    self.send_json({"error": str(e)}, e.code)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:
                    traceback.print_exc()
                    try:
                        self.send_json(
                            {"error": "服务器内部错误: %s" % e}, 500)
                    except Exception:
                        pass

            def _try_static(self, path: str) -> bool:
                rel = unquote(path)
                if rel in ("/", "/index.html"):
                    rel = "/index.html"
                full = os.path.realpath(
                    os.path.join(app.static_dir, rel.lstrip("/")))
                if not full.startswith(os.path.realpath(app.static_dir) + os.sep) \
                        and full != os.path.realpath(app.static_dir):
                    return False
                if not os.path.isfile(full):
                    return False
                self.send_file(full, cache=False)
                return True

            def _read_body(self, method):
                if method not in ("POST", "PUT", "DELETE"):
                    return {}
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return {}
                if length > MAX_BODY:
                    raise HttpError("请求体过大（上限 %d MB）"
                                    % (MAX_BODY // 1048576), 413)
                raw = self.rfile.read(length)
                ctype = self.headers.get("Content-Type") or ""
                if "multipart/form-data" in ctype.lower():
                    # boundary 大小写敏感，必须传原始头值
                    return parse_multipart(raw, ctype)
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    return {}

            def _ws_handshake(self):
                from shared.ws import upgrade_handshake
                return upgrade_handshake(self)

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def do_DELETE(self):
                self._dispatch("DELETE")

            def do_HEAD(self):
                self._dispatch("HEAD")

        return Handler

    def serve_forever(self, host: str, port: int):
        # Windows 下 SO_REUSEADDR 允许多进程绑定同一端口（请求被随机
        # 分发给不同实例，造成任务状态不一致 / 卡死）——Windows 显式关闭，
        # 端口被占用时启动直接失败，避免多实例并存。
        # Linux / macOS 保留 SO_REUSEADDR：否则上一次连接的 TIME-WAIT
        # 残留会让服务重启失败（Address already in use）。
        ThreadingHTTPServer.allow_reuse_address = (os.name != "nt")
        httpd = ThreadingHTTPServer((host, port), self._handler_cls)
        httpd.daemon_threads = True
        self.httpd = httpd
        httpd.serve_forever(poll_interval=0.4)

    def shutdown(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass


class _Ctx:
    """传给路由处理器的响应工具集（包装 handler）。"""

    def __init__(self, handler):
        self.h = handler

    @property
    def headers(self):
        return self.h.headers

    def json(self, obj, code=200):
        self.h.send_json(obj, code)

    def file(self, path, cache=False, download=""):
        self.h.send_file(path, cache=cache, download=download)

    def error(self, msg: str, code: int = 400):
        raise HttpError(msg, code)
