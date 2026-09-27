"""ws —— RFC6455 WebSocket 实现（仅标准库）。

服务端：WSHub 管理多客户端连接，支持 JSON 文本广播与二进制广播
（二进制消息 = 4 字节大端事件类型 + 载荷，与 ComfyUI 预览帧协议一致）。
客户端：ws_connect 用于应用后端订阅引擎事件（带掩码帧）。

连接对象的读写分属不同线程：httpd 工作线程完成握手后接管读，
后台发送线程从每连接队列取数据写 socket，避免广播阻塞读循环。
"""
import base64
import hashlib
import json
import os
import queue
import select
import socket
import struct
import threading
import time
import urllib.parse

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OP_TEXT, _OP_BIN, _OP_CLOSE, _OP_PING, _OP_PONG = 0x1, 0x2, 0x8, 0x9, 0xA

MAX_FRAME = 64 * 1024 * 1024          # 单帧上限 64MB（预览图足够）


class WSError(Exception):
    pass


# ---------------------------------------------------------------- 帧编解码

def _encode_frame(payload: bytes, opcode: int, mask: bool = False) -> bytes:
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n if mask else n)
    elif n < 65536:
        header.append(0x80 | 126 if mask else 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127 if mask else 127)
        header += struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        header += key
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + payload


class _SockReader:
    """超时安全的套接字读取器（select + raw recv，带内部缓冲）。

    socket.makefile 返回的缓冲读对象在超时模式下一次超时后即永久失效
    （"cannot read from timed out object"）；这里用 select 控制单次
    deadline、原始 recv 取数据，超时不会破坏套接字与缓冲状态。
    """

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buf = bytearray()

    def read(self, n: int, timeout: float = 1.0) -> bytes:
        """精确读 n 字节；deadline 内不够则抛 socket.timeout。"""
        if n <= 0:
            return b""
        deadline = time.monotonic() + max(0.001, timeout)
        while len(self._buf) < n:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise socket.timeout()
            r, _, _ = select.select([self.sock], [], [], remain)
            if not r:
                raise socket.timeout()
            try:
                chunk = self.sock.recv(min(65536, n - len(self._buf)))
            except socket.timeout:
                raise
            except OSError as e:
                raise WSError("连接中断: %s" % e) from None
            if not chunk:
                raise WSError("连接中断")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


def _read_frame(reader, mask_expected: bool, timeout: float = 1.0):
    """读一条完整帧。返回 (opcode, payload)；reader 为 _SockReader。

    fin 帧才返回；ping 自动回由调用方处理；分片自动拼接。
    """
    opcodes, payload_all = [], bytearray()
    while True:
        hdr = reader.read(2, timeout)
        fin, opcode = bool(hdr[0] & 0x80), hdr[0] & 0x0F
        masked, length = bool(hdr[1] & 0x80), hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", reader.read(2, timeout))[0]
        elif length == 127:
            length = struct.unpack(">Q", reader.read(8, timeout))[0]
        if length > MAX_FRAME:
            raise WSError("帧过大")
        if masked != mask_expected:
            raise WSError("掩码状态异常")
        key = reader.read(4, timeout) if masked else b""
        payload = reader.read(length, timeout) if length else b""
        if masked:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        if opcode == _OP_PING:
            opcodes.append(_OP_PONG)
            payload_all += payload
            continue
        if opcode == _OP_PONG or opcode == _OP_CLOSE:
            if opcode == _OP_CLOSE:
                return _OP_CLOSE, payload
            continue
        opcodes.append(opcode)
        payload_all += payload
        if fin:
            return opcodes[0], bytes(payload_all)


# ---------------------------------------------------------------- 服务端

class WSConn:
    """单个 WebSocket 连接。"""

    def __init__(self, sock: socket.socket, sid: str):
        self.sid = sid
        self.sock = sock
        self.reader = _SockReader(sock)
        self.alive = True
        self._txq: "queue.Queue[bytes | None]" = queue.Queue(maxsize=256)
        self._tx = threading.Thread(target=self._sender, daemon=True)
        self._tx.start()

    def _sender(self):
        while True:
            item = self._txq.get()
            if item is None:
                break
            try:
                self.sock.sendall(item)
            except Exception:
                self.alive = False
                break

    def _push(self, data: bytes):
        if not self.alive:
            return
        try:
            self._txq.put_nowait(data)
        except queue.Full:
            self.alive = False

    def send_json(self, event, data):
        self._push(_encode_frame(
            json.dumps({"type": event, "data": data},
                       ensure_ascii=False).encode("utf-8"), _OP_TEXT))

    def send_binary(self, event_type: int, data: bytes):
        self._push(_encode_frame(struct.pack(">I", event_type) + data, _OP_BIN))

    def recv_text(self, timeout: float = 1.0):
        """带超时地读一条文本消息；超时返回 None；关闭/错误抛 WSError。"""
        try:
            op, payload = _read_frame(self.reader, mask_expected=True,
                                      timeout=timeout)
        except socket.timeout:
            return None
        if op == _OP_CLOSE:
            raise WSError("客户端关闭")
        if op == _OP_TEXT:
            return payload.decode("utf-8", "replace")
        return None

    def send_pong_now(self, payload: bytes = b""):
        """直接在当前线程回 pong（读循环里用，绕过发送队列）。"""
        try:
            self.sock.sendall(_encode_frame(payload, _OP_PONG))
        except Exception:
            self.alive = False

    def close(self):
        self.alive = False
        try:
            self._txq.put_nowait(None)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def upgrade_handshake(handler) -> WSConn:
    """在 BaseHTTPRequestHandler 上完成 WebSocket 升级握手，返回连接对象。"""
    key = handler.headers.get("Sec-WebSocket-Key", "")
    accept = base64.b64encode(
        hashlib.sha1((key + _GUID).encode()).digest()).decode()
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()
    sid = hashlib.md5((key + str(time.time())).encode()).hexdigest()[:12]
    return WSConn(handler.connection, sid)


class WSHub:
    """多客户端广播中心。"""

    def __init__(self):
        self._conns: dict[str, WSConn] = {}
        self._lock = threading.Lock()

    def add(self, conn: WSConn):
        with self._lock:
            self._conns[conn.sid] = conn

    def remove(self, conn: WSConn):
        with self._lock:
            self._conns.pop(conn.sid, None)
        conn.close()

    @property
    def count(self) -> int:
        return len(self._conns)

    def broadcast_json(self, event: str, data, sid: str | None = None):
        """sid 为 None 广播全部；否则只发给该客户端。"""
        with self._lock:
            targets = [self._conns[sid]] if sid and sid in self._conns \
                else list(self._conns.values()) if not sid else []
        for c in targets:
            c.send_json(event, data)

    def broadcast_binary(self, event_type: int, data: bytes, sid: str | None = None):
        with self._lock:
            targets = [self._conns[sid]] if sid and sid in self._conns \
                else list(self._conns.values()) if not sid else []
        for c in targets:
            c.send_binary(event_type, data)


# ---------------------------------------------------------------- 客户端

class WSClient:
    """极简 WebSocket 客户端（用于订阅引擎事件）。"""

    def __init__(self, url: str):
        u = urllib.parse.urlparse(url)
        self.host, self.port = u.hostname, u.port or 80
        self.path = (u.path or "/") + (("?" + u.query) if u.query else "")
        self.sock: socket.socket | None = None
        self.reader: _SockReader | None = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        self.sock.settimeout(None)
        self.reader = _SockReader(self.sock)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n"
               % (self.path, self.host, self.port, key))
        self.sock.sendall(req.encode())
        # 读响应头（逐字节经 reader，剩余字节留在缓冲供后续帧读取）
        status_line = self._read_header_line()
        if " 101 " not in status_line:
            raise WSError("WebSocket 握手失败: %s" % status_line.strip())
        while True:
            line = self._read_header_line()
            if not line or line in ("\r\n", "\n"):
                break
        return self

    def _read_header_line(self) -> str:
        buf = bytearray()
        while not buf.endswith(b"\n"):
            buf += self.reader.read(1, timeout=10.0)
        return buf.decode("latin1")

    def recv(self, timeout: float = 5.0):
        """收一条消息。返回 ("text", str) / ("binary", int_event, bytes)。"""
        if not self.reader:
            raise WSError("未连接")
        try:
            op, payload = _read_frame(self.reader, mask_expected=False,
                                      timeout=timeout)
        except socket.timeout:
            return ("timeout", None)
        if op == _OP_CLOSE:
            raise WSError("服务端关闭连接")
        if op == _OP_TEXT:
            return ("text", json.loads(payload.decode("utf-8")))
        if op == _OP_BIN and len(payload) >= 4:
            return ("binary", struct.unpack(">I", payload[:4])[0], payload[4:])
        return ("other", None)

    def send_json(self, event: str, data):
        msg = json.dumps({"type": event, "data": data}, ensure_ascii=False)
        self.sock.sendall(_encode_frame(msg.encode("utf-8"), _OP_TEXT, mask=True))

    def close(self):
        try:
            if self.sock:
                self.sock.sendall(_encode_frame(b"", _OP_CLOSE, mask=True))
        except Exception:
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None
