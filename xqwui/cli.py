"""cli —— 命令行服务生命周期管理（启动 / 停止 / 状态 / 重启）。

用法：python -m xqwui.cli <command> [options]

命令设计：
  start    启动服务。默认前台运行；-d 后台守护运行（输出重定向到日志目录）。
           已在运行时直接提示并成功退出（幂等，不重复起实例）。
  stop     优雅停止服务。POSIX 发 SIGTERM（服务内部先停 HTTP 再释放调度器
           与连接）；Windows 下无法向独立会话进程投递控制台信号，等待宽限
           期后 taskkill 强制结束（任务状态已持久化，安全）。未运行时仅
           清理残留 pid 记录并成功退出（幂等）。
  status   查询运行状态：pid 文件进程存活 + HTTP 健康探针双重确认。
  restart  先 stop 再 start（沿用 start 参数）。

pid 记录：data/xqwui.pid（JSON：pid / host / port / started_ms）。
退出码：0 成功；1 操作失败；2 参数错误；3 status 时服务未运行。

信号处理（服务进程）：SIGTERM / SIGINT（Ctrl+C）/ SIGBREAK → 停 HTTP 接收、
停止调度器线程与引擎 WS → 写日志退出；pid 文件随手清理。
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXIT_OK, EXIT_FAIL, EXIT_ARGS, EXIT_STOPPED = 0, 1, 2, 3
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


# ---------------------------------------------------------------- pid 记录
def _pid_file() -> str:
    from xqwui import config as appcfg
    return os.path.join(appcfg.DATA_DIR, "xqwui.pid")


def _read_pid() -> dict | None:
    try:
        with open(_pid_file(), "r", encoding="utf-8") as f:
            d = json.loads(f.read() or "{}")
        return d if isinstance(d, dict) and int(d.get("pid") or 0) > 0 else None
    except (OSError, ValueError):
        return None


def _write_pid(host: str, port: int) -> None:
    from shared.util import atomic_write_json
    atomic_write_json(_pid_file(), {
        "pid": os.getpid(), "host": host, "port": port,
        "started_ms": int(time.time() * 1000)})


def _remove_pid() -> None:
    try:
        os.remove(_pid_file())
    except OSError:
        pass


# ---------------------------------------------------------------- 探测
def _pid_alive(pid: int) -> bool:
    """进程存活检查。Windows 禁用 os.kill(pid, 0)（会误杀进程），
    用 OpenProcess + GetExitCodeProcess；POSIX 用 kill 0 信号探测。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                                # 存在但无权发信号
    except OSError:
        return False


def _health(port: int, timeout: float = 1.5) -> dict | None:
    """HTTP 健康探针：确认该端口上确是本服务（而非其他程序占用）。"""
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/system/health" % port,
                timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8") or "{}")
        return d if isinstance(d, dict) and d.get("ok") else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _status_full(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/system/status" % port,
                timeout=2.0) as r:
            return json.loads(r.read().decode("utf-8") or "{}") or None
    except (urllib.error.URLError, OSError, ValueError):
        return None


# ---------------------------------------------------------------- 环境装配
def _apply_env(args) -> None:
    """必须在首次导入 xqwui.config 之前调用（DEFAULTS / CONFIG_FILE 在
    模块导入时读取环境变量）。"""
    if getattr(args, "config", None):
        os.environ["XQWUI_CONFIG_FILE"] = os.path.abspath(args.config)
    if getattr(args, "log_level", None):
        os.environ["XQWUI_LOG_LEVEL"] = str(args.log_level).upper()
    if getattr(args, "port", None):
        os.environ["XQWUI_PORT"] = str(args.port)
    if getattr(args, "host", None):
        os.environ["XQWUI_HOST"] = str(args.host)


def _serve(args) -> int:
    """服务主体（前台进程 / 守护子进程共用）：装配 + 信号处理 + 运行。"""
    from xqwui import config as appcfg
    from xqwui import logutil
    from xqwui.main import XQWUIApp, _busy_hint, _port_free
    from xqwui.scheduler import get_manager

    appcfg.ensure_dirs()
    logutil.setup(appcfg.log_dir())
    c = appcfg.load()
    host, port = str(args.host or c["host"]), int(args.port or c["port"])
    if not _port_free(host, port):
        print("启动失败：端口 %d 已被占用 —— %s" % (port, _busy_hint(port)),
              file=sys.stderr)
        return EXIT_FAIL

    import threading
    app, mgr = XQWUIApp(), get_manager()
    mgr.start()
    _write_pid(host, port)
    print("XQWUI 已启动：http://%s:%d（pid=%d）"
          % ("127.0.0.1" if host == "0.0.0.0" else host, port, os.getpid()))

    def _graceful(signum, frame):
        # shutdown() 会等待 serve_forever 退出，不能在信号处理线程内直接调
        threading.Thread(target=app.shutdown, daemon=True).start()

    for sig in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, sig):
            try:
                signal.signal(getattr(signal, sig), _graceful)
            except (ValueError, OSError):
                pass                                 # 非主线程注册等场景忽略

    try:
        app.serve_forever(host, port)
        return EXIT_OK
    except OSError as e:
        print("服务异常退出：%s" % e, file=sys.stderr)
        return EXIT_FAIL
    finally:
        mgr.stop()                                   # 调度器线程 + 引擎 WS
        _remove_pid()
        from xqwui import logutil as _l
        _l.get("cli").info("服务已停止（pid=%d）", os.getpid())


# ---------------------------------------------------------------- 命令
def cmd_start(args) -> int:
    _apply_env(args)
    from xqwui import config as appcfg
    appcfg.ensure_dirs()

    # 幂等：pid 记录存活且健康探针通过 → 已在运行，直接成功返回
    info = _read_pid()
    if info and _pid_alive(info["pid"]) and _health(info["port"]):
        print("服务已在运行（pid=%d，端口 %d），无需重复启动 —— "
              "http://127.0.0.1:%d" % (info["pid"], info["port"], info["port"]))
        return EXIT_OK

    if not args.daemon:
        return _serve(args)                          # 前台：本进程直接运行

    # 守护模式预检：端口已被占用时区分「已有 XQWUI 实例」与「其他程序」
    from xqwui.main import _busy_hint, _port_free
    port = int(args.port or appcfg.load()["port"])
    host = str(args.host or appcfg.load()["host"])
    if not _port_free(host, port):
        if _health(port):
            print("已有运行中的 XQWUI 实例（未经本 CLI 启动，无 pid 记录）——"
                  "直接访问 http://127.0.0.1:%d 即可，无需重复启动" % port)
            return EXIT_OK                           # 幂等
        print("启动失败：端口 %d 已被占用 —— %s" % (port, _busy_hint(port)),
              file=sys.stderr)
        return EXIT_FAIL

    # 派生独立子进程运行，等待「pid 记录 + 健康探针」双确认就绪
    cmd = [sys.executable, "-m", "xqwui.cli", "_run",
           "--host", str(args.host or ""), "--port", str(args.port or "")]
    if getattr(args, "config", None):
        cmd += ["--config", os.path.abspath(args.config)]
    if getattr(args, "log_level", None):
        cmd += ["--log-level", str(args.log_level).upper()]
    kw = {"stdin": subprocess.DEVNULL, "cwd": os.getcwd()}
    if os.name == "nt":
        flag = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kw["creationflags"] = flag
    else:
        kw["start_new_session"] = True
    try:
        logf = open(os.path.join(appcfg.log_dir(), "cli-daemon.log"),
                    "ab")
    except OSError:
        logf = subprocess.DEVNULL
    try:
        subprocess.Popen(cmd, stdout=logf, stderr=logf, **kw)
    finally:
        if logf not in (subprocess.DEVNULL,):
            logf.close()

    deadline = time.time() + max(3.0, float(args.wait))
    while time.time() < deadline:
        time.sleep(0.5)
        info = _read_pid()                           # 子进程成功绑定后写入
        if info and _pid_alive(info["pid"]) and _health(info["port"]):
            print("服务已后台启动：http://127.0.0.1:%d（pid=%d）"
                  % (info["port"], info["pid"]))
            return EXIT_OK
    print("启动超时：%.0f 秒内健康探针未就绪 —— 请查看日志 %s"
          % (args.wait, appcfg.log_dir()), file=sys.stderr)
    return EXIT_FAIL


def cmd_stop(args) -> int:
    info = _read_pid()
    if not info:
        print("服务未在运行（无 pid 记录）")
        return EXIT_OK                               # 幂等
    pid, port = int(info["pid"]), int(info.get("port") or 0)
    if not _pid_alive(pid):
        _remove_pid()
        print("服务未在运行（已清理残留 pid 记录 pid=%d）" % pid)
        return EXIT_OK                               # 幂等

    # 优雅停止：POSIX 发 SIGTERM；Windows 独立会话收不到控制台信号，
    # 先礼貌尝试 taskkill（WM_CLOSE），宽限后强制结束
    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            print("停止失败：无法向 pid=%d 发送信号（%s）" % (pid, e),
                  file=sys.stderr)
            return EXIT_FAIL
    else:
        subprocess.run(["taskkill", "/PID", str(pid)],
                       capture_output=True)

    deadline = time.time() + max(1.0, float(args.timeout))
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.3)
    if _pid_alive(pid):
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True)
        else:
            os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
    if _pid_alive(pid):
        print("停止失败：进程 %d 未能退出，请手动处理" % pid, file=sys.stderr)
        return EXIT_FAIL
    _remove_pid()
    print("服务已停止（pid=%d）" % pid)
    return EXIT_OK


def cmd_status() -> int:
    info = _read_pid()
    if info and _pid_alive(info["pid"]):
        st = _status_full(int(info.get("port") or 0)) or {}
        eng = st.get("engine") or {}
        print("运行中")
        print("  pid     = %d" % info["pid"])
        print("  地址    = http://127.0.0.1:%s" % info.get("port", "?"))
        if st.get("app"):
            print("  运行时长= %d 分钟" % int((st["app"].get("uptime_s") or 0) / 60))
        print("  引擎    = %s" % ("在线" if eng.get("reachable") else "离线"))
        return EXIT_OK
    if info:
        _remove_pid()
        print("未运行（pid 记录已失效，pid=%d）" % info["pid"])
    else:
        print("未运行")
    return EXIT_STOPPED


def cmd_restart(args) -> int:
    rc = cmd_stop(args)
    if rc != EXIT_OK:
        return rc
    return cmd_start(args)


# ---------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m xqwui.cli",
        description="XQWUI 服务生命周期管理（start / stop / restart / status）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("start", help="启动服务（默认前台；-d 后台守护）")
    sp.add_argument("-H", "--host", default="", help="监听地址（默认取配置）")
    sp.add_argument("-p", "--port", type=int, default=0, help="监听端口（默认取配置）")
    sp.add_argument("-c", "--config", default="", help="配置文件路径（默认 data/config.json）")
    sp.add_argument("-l", "--log-level", default="", choices=_LOG_LEVELS,
                    help="日志级别（默认取配置 / INFO）")
    sp.add_argument("-d", "--daemon", action="store_true", help="后台守护运行")
    sp.add_argument("--wait", type=float, default=20.0,
                    help="后台模式等待就绪的超时秒数（默认 20）")
    sp.set_defaults(fn=cmd_start)

    sp = sub.add_parser("stop", help="停止服务（优雅优先，超时强制）")
    sp.add_argument("--timeout", type=float, default=10.0,
                    help="优雅退出宽限秒数（默认 10，超时强制结束）")
    sp.set_defaults(fn=cmd_stop)

    sp = sub.add_parser("restart", help="重启服务（stop + start）")
    sp.add_argument("-H", "--host", default="")
    sp.add_argument("-p", "--port", type=int, default=0)
    sp.add_argument("-c", "--config", default="")
    sp.add_argument("-l", "--log-level", default="", choices=_LOG_LEVELS)
    sp.add_argument("-d", "--daemon", action="store_true")
    sp.add_argument("--wait", type=float, default=20.0)
    sp.add_argument("--timeout", type=float, default=10.0)
    sp.set_defaults(fn=cmd_restart)

    sub.add_parser("status", help="查询运行状态（退出码 0=运行中 3=未运行）")

    sp = sub.add_parser("_run", help=argparse.SUPPRESS)   # 内部：守护子进程入口
    sp.add_argument("--host", default="")
    sp.add_argument("--port", default="")
    sp.add_argument("--config", default="")
    sp.add_argument("--log-level", default="")
    sp.set_defaults(fn=lambda a: _serve(a))

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if getattr(args, "cmd", "") == "status":
            return cmd_status()
        return args.fn(args)
    except KeyboardInterrupt:
        print("\n已取消")
        return EXIT_FAIL
    except (OSError, ValueError) as e:
        print("命令执行失败：%s" % e, file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main() or 0)
