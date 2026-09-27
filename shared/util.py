"""util —— 通用工具：原子写 JSON、路径安全边界、文件名净化、格式化。"""
import json
import os
import re
import shutil
import threading
import time


def now_ms() -> int:
    """当前毫秒时间戳（与 ComfyUI extra_data.create_time 语义一致）。"""
    return int(time.time() * 1000)


# ------------------------------------------------------------ Windows 句柄共享
# Windows 下 CPython 的普通 open() 不含 FILE_SHARE_DELETE：一个正在读文件的
# 句柄会让其他进程/线程的 os.replace 报 WinError 5（拒绝访问）——任务进度
# 落盘与任务列表读取并发时即触发，曾致运行中任务被判 error。
_NT = os.name == "nt"


def _open_read(path: str):
    """读模式文件对象；Windows 以 FILE_SHARE_READ|WRITE|DELETE 打开，使读句柄
    不再阻塞他人对同一路径的原子替换。不支持的环境/异常时退回普通 open。"""
    if _NT:
        try:
            import ctypes
            import msvcrt
            CreateFileW = ctypes.windll.kernel32.CreateFileW
            CreateFileW.restype = ctypes.c_void_p   # Win64 句柄不可截断
            h = CreateFileW(
                str(path), 0x80000000,        # GENERIC_READ
                0x1 | 0x2 | 0x4,              # SHARE_READ|WRITE|DELETE
                None, 3, 0, None)             # OPEN_EXISTING
            if h and h != 2 ** 64 - 1:       # 非 INVALID_HANDLE_VALUE
                return os.fdopen(msvcrt.open_osfhandle(h, os.O_RDONLY), "rb")
        except Exception:
            pass
    return open(path, "rb")


def _open_write_shared(path: str):
    """写模式文件对象；Windows 带 FILE_SHARE_READ|WRITE——直写兜底 / 临时
    文件窗口期不阻塞他人读（与 _open_read 对称）。异常时退回普通 open。"""
    if _NT:
        try:
            import ctypes
            import msvcrt
            CreateFileW = ctypes.windll.kernel32.CreateFileW
            CreateFileW.restype = ctypes.c_void_p
            h = CreateFileW(
                str(path), 0x40000000,        # GENERIC_WRITE
                0x1 | 0x2,                    # SHARE_READ|WRITE
                None, 2, 0, None)             # CREATE_ALWAYS
            if h and h != 2 ** 64 - 1:
                return os.fdopen(msvcrt.open_osfhandle(h, os.O_WRONLY), "wb")
        except Exception:
            pass
    return open(path, "wb")


def _tmp_name(path: str) -> str:
    """同一路径的并发写各自持有独立 tmp（list() 仍按 .tmp 后缀过滤）。"""
    return "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())


def _safe_unlink(p: str) -> None:
    try:
        os.unlink(p)
    except OSError:
        pass


def _write_all(path: str, data: bytes) -> None:
    with _open_write_shared(path) as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def atomic_write_bytes(path: str, data: bytes) -> None:
    """tmp + os.replace 原子落盘；Windows 瞬时占用（杀毒扫描 / IDE 打开 /
    并发读句柄）时短暂退避重试，重试耗尽后直写目标兜底——尽力保住可用性，
    仅牺牲极端场景（写一半断电）下的原子性。
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = _tmp_name(path)
    last: OSError | None = None
    for attempt in range(12):
        try:
            _write_all(tmp, data)
            os.replace(tmp, path)
            return
        except OSError as e:
            last = e
            _safe_unlink(tmp)
            time.sleep(min(0.4, 0.05 * (attempt + 1)))
    try:
        _write_all(path, data)
        return
    except OSError:
        pass
    if last:
        raise last


def atomic_copy_file(src: str, path: str) -> None:
    """大文件分块拷贝 + 原子替换，重试/兜底语义同 atomic_write_bytes。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

    def _cp(dst: str) -> None:
        with open(src, "rb") as fi, _open_write_shared(dst) as fo:
            shutil.copyfileobj(fi, fo, 4 * 1024 * 1024)
            fo.flush()
            os.fsync(fo.fileno())

    tmp = _tmp_name(path)
    last: OSError | None = None
    for attempt in range(12):
        try:
            _cp(tmp)
            os.replace(tmp, path)
            return
        except OSError as e:
            last = e
            _safe_unlink(tmp)
            time.sleep(min(0.4, 0.05 * (attempt + 1)))
    try:
        _cp(path)
        return
    except OSError:
        pass
    if last:
        raise last


def atomic_write_json(path: str, obj) -> None:
    """见 atomic_write_bytes；JSON 序列化后走同一条加固通道。"""
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def read_json(path, default=None):
    """读取 JSON；文件缺失或损坏时返回 default。"""
    try:
        with _open_read(path) as f:
            return json.loads(f.read())
    except Exception:
        return default


def inside(path: str, base: str) -> bool:
    """realpath 解析后判断 path 是否位于 base 目录内（防路径穿越）。"""
    try:
        p = os.path.realpath(path)
        b = os.path.realpath(base)
        return os.path.commonpath([p, b]) == b
    except Exception:
        return False


_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def safe_name(name: str, max_len: int = 80) -> str:
    """净化为安全文件名：去路径分隔符与保留字符，禁止下划线开头
    （下划线开头保留给系统文件，如自动存档 _autosave.json）。"""
    safe = _UNSAFE.sub("_", str(name or "")).strip(" .")[:max_len]
    return safe.lstrip("_").strip(" .")


def human_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.1f %s" if unit != "B" else "%.0f %s") % (n, unit)
        n /= 1024.0
    return "-"


def fmt_ts(ms) -> str:
    """毫秒时间戳 → 本地 'MM-DD HH:MM:SS'。"""
    if not ms:
        return "-"
    t = time.localtime(float(ms) / 1000.0)
    return time.strftime("%m-%d %H:%M:%S", t)
