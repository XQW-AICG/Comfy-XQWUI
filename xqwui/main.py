"""main —— 应用后端启动入口。

用法：python -m xqwui.main
默认 0.0.0.0:8900（XQWUI_HOST / XQWUI_PORT 可覆盖）。

装配：
  shared.httpd.App   路由基座 + web/ 静态目录
  xqwui.api.*        REST 路由（任务/系统/素材成品/预设/存储）
  SchedulerManager   任务调度器（引擎 WS 订阅 + 单工作线程）
  WSHub + EventBus   /ws 实时事件推送（任务状态/进度/引擎连接）
"""
import errno
import json
import logging
import os
import socket
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.httpd import App                       # noqa: E402
from shared.ws import WSHub, WSError               # noqa: E402

from xqwui import config as appcfg                 # noqa: E402
from xqwui import events                           # noqa: E402
from xqwui import logutil                          # noqa: E402
from xqwui.api import register_all                 # noqa: E402
from xqwui.scheduler import get_manager            # noqa: E402

LOG = logutil.get("main")


class XQWUIApp(App):
    def __init__(self):
        super().__init__(static_dir=appcfg.WEB_DIR)
        self.hub = WSHub()
        register_all(self)
        self.guard(self._guard)
        self.on_websocket(self._handle_ws)
        # 事件总线 → WS 广播桥
        events.bus.subscribe(
            lambda evt: self.hub.broadcast_json(evt["type"], evt["data"]))

    # ------------------------------------------------------------ 访问口令
    def _guard(self, handler, q) -> bool:
        token = str(appcfg.load().get("token") or "")
        if not token:
            return True
        ck = handler.headers.get("Cookie") or ""
        if ("ctoken=" + token) in ck:
            return True
        if q.get("token") == token:
            handler.send_response(302)
            handler.send_header(
                "Set-Cookie",
                "ctoken=%s; Path=/; Max-Age=2592000; SameSite=Lax" % token)
            handler.send_header("Location",
                                handler.path.split("?")[0] or "/")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return False
        handler.send_json({"error": "需要访问口令（?token=你的口令）"}, 401)
        return False

    # ------------------------------------------------------------ WebSocket
    def _handle_ws(self, conn, q):
        self.hub.add(conn)
        mgr = get_manager()
        try:
            conn.send_json("hello", {
                "tasks": mgr.summaries(),
                "engine": mgr.engine_status()})
            while True:
                msg = conn.recv_text(timeout=30.0)
                if msg is None:               # 超时无消息：继续保活
                    continue
                if "ping" in msg:
                    conn.send_pong_now()
        except WSError:
            pass
        finally:
            self.hub.remove(conn)


def _port_free(host: str, port: int) -> bool:
    """启动前端口预检（试绑定后立刻关闭）。

    先起调度器再因端口占用退出会有窗口期：两个实例同时驱动同一份任务
    目录与引擎队列；故在 mgr.start() 之前先确认端口可用。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":                       # 与 httpd 一致：仅非 Windows 复用
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _busy_hint(port: int) -> str:
    """端口被占用时探测该端口上是否已是运行中的 XQWUI 实例。

    是 → 直接访问即可，无需再启一个（多实例会争抢同一个任务目录与
    引擎队列）；否 / 探测不通 → 返回换端口或停旧进程的操作提示。
    """
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/system/status" % port,
                timeout=1.5) as r:
            d = json.loads(r.read().decode("utf-8") or "{}")
        if isinstance(d, dict) and isinstance(d.get("app"), dict):
            return ("该端口上已有运行中的 XQWUI 实例（已运行 %s 秒），"
                    "直接访问 http://127.0.0.1:%d 即可，无需重复启动"
                    % (d["app"].get("uptime_s", "?"), port))
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return ("若确认旧进程已退出，多为端口处于 TIME-WAIT，稍等重试；"
            "否则先停止占用进程，或改 data/config.json 的 port 后重启"
            "（独立数据目录可用 XQWUI_PORT=<端口>）")


def main():
    appcfg.ensure_dirs()
    logutil.setup(appcfg.log_dir())     # 级别与切割策略取自 config.json
    c = appcfg.load()
    host, port = c["host"], int(c["port"])
    if not _port_free(host, port):
        LOG.error("启动失败：端口 %d 已被占用 —— %s", port, _busy_hint(port))
        return 1                              # 调度器尚未启动，直接退出
    app = XQWUIApp()
    mgr = get_manager()
    mgr.start()
    LOG.info("=" * 60)
    LOG.info("XQWUI 漫剧工作台")
    LOG.info("地址=http://%s:%d", "127.0.0.1" if host == "0.0.0.0" else host,
             port)
    LOG.info("引擎=%s（Comfy 后端 API 规范）", c["engine_url"])
    LOG.info("数据=%s 日志=%s", appcfg.DATA_DIR, appcfg.log_dir())
    lc = logutil.log_config()
    LOG.info("日志切割=%.0fMB/%s 保留=%d份/%.0fMB 归档=%s 级别=%s",
             lc["max_mb"], lc["when"], lc["keep_files"], lc["max_total_mb"],
             "gzip" if lc["gzip"] else "关", lc["level"])
    if c.get("token"):
        LOG.info("口令=已启用（首次访问请带 ?token=）")
    LOG.info("=" * 60)
    try:
        app.serve_forever(host, port)
    except KeyboardInterrupt:
        pass
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            LOG.error("启动失败：端口 %d 已被占用 —— %s", port, _busy_hint(port))
        else:
            LOG.exception("启动失败：监听 %s:%d 异常: %s", host, port, e)
    finally:
        mgr.stop()
        LOG.info("服务已停止")


if __name__ == "__main__":
    sys.exit(main() or 0)
