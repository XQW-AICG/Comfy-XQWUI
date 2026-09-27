"""logutil —— 统一日志：控制台 + 自动切割文件（data/logs/xqwui.log）。

格式（控制台与文件一致）：
    MM-dd HH:MM:SS | LEVEL | xqwui.模块 | 消息

消息正文约定为「键=值」结构化字段，便于 grep / 检索：
    task=<任务ID> op=<操作> seg=<段号> 原因=<...>

级别约定：
    DEBUG  执行细节（进度采样、素材清单等；需 log_level=DEBUG 开启）
    INFO   任务生命周期（创建 / 开始 / 段提交与完成 / 合并完成 / 控制操作）
    WARN   可自动恢复的异常（引擎断线重连、ffmpeg 回退重编码、操作被拒绝）
    ERROR  任务失败 / 调度器内部错误（原因必带；内部错误附完整堆栈）

性能：全部使用 %-style 惰性格式化（未输出的 DEBUG 不做字符串拼接）；
高频路径（进度事件）以 isEnabledFor 预判后再构造参数。

==========================================================================
日志切割方案（自动切割 / 命名 / 保留 / 归档）
==========================================================================

1) 触发条件：大小与时间双触发，任一先到即切
   大小  当前 xqwui.log 体积 >= log_rotate_max_mb（默认 20MB）
   时间  跨过 log_rotate_when 定义的时间边界
         off      不按时间切割
         hourly   每小时整点
         midnight 每日 00:00（默认）
   判定发生在每次写入前（shouldRollover），与日志级别无关。

2) 命名规则
   当前文件  xqwui.log                        （唯一被写入的文件，路径恒定）
   历史文件  xqwui-<YYYYmmdd>-<HHMMSS>.log     （切割时刻的本地时间）
   同名冲突  xqwui-<YYYYmmdd>-<HHMMSS>-2.log   （同一秒内多次切割，序号递增）
   压缩归档  xqwui-<YYYYmmdd>-<HHMMSS>.log.gz  （log_archive_gzip=1 时）
   文件名定长时间戳前缀 → 字典序即时间序，排序 / 检索不依赖 mtime。

3) 保留策略：份数与总容量两个上限同时生效，取更严格者
   份数  log_keep_files    默认 10 份（不含当前文件；0 = 不保留历史）
   容量  log_max_total_mb  默认 200MB（logs 目录全部文件；0 = 不限）
   超限后从最旧的历史文件开始删除，直到两个上限都满足；
   当前文件永不被自动删除（只能手动清空），保证写入目标始终存在。

4) 归档 / 清理机制
   归档  切割后由后台守护线程 gzip 压缩历史文件（.log → .log.gz 后删除原文件），
        压缩不在日志锁内进行，不阻塞业务写入；进程退出时未压缩完的文件
        保持 .log 原样，下次启动仍可被识别、清理与查看。
   清理  每次切割后自动 prune；启动时 prune 一次；API 可手动触发。
   删除只对匹配 xqwui-<时间戳>.log[.gz] 的历史文件生效，目录内其它文件
   只计入容量统计、不会被删除。

5) 切割对正在写入的日志的影响
   切割全程在 logging 的线程锁（RLock）内完成：flush → close → rename →
   重新 open → 写切割标记。同目录 rename 是原子操作，耗时在毫秒级。
   —— 同一进程内其它线程的 emit 会在锁上短暂排队后写入新文件：
      不丢日志、不抛异常、不产生交错内容（锁保证串行）。
   —— 外部进程（tail -f / 编辑器）持有的是旧 inode：POSIX 下继续读旧文件
      直到关闭，不受 rename 影响（tail -F 可自动跟随新文件）。
   —— Windows 下若旧文件被其它进程独占（无 FILE_SHARE_DELETE），rename
      会失败：此时不抛异常、不丢日志，改为原地续写并 30 秒后重试切割，
      同时向 stderr 输出一行提示。
   切割后新文件首行写入标记行（log_rotate_marker=1）：
       2026-09-27 03:42:00 | 日志切割 触发=size 上一文件=... 大小=20.0MB
   便于人工确认切割点与触发原因。

6) 可配置性（data/config.json，支持环境变量覆盖，热更新无需重启）
   log_rotate_max_mb  单文件上限 MB（0 = 不按大小切割）  XQWUI_LOG_ROTATE_MAX_MB
   log_rotate_when    时间策略 off/hourly/midnight        XQWUI_LOG_ROTATE_WHEN
   log_keep_files     历史保留份数（0 = 不保留）          XQWUI_LOG_KEEP_FILES
   log_max_total_mb   logs 目录总占用上限 MB（0 = 不限）  XQWUI_LOG_MAX_TOTAL_MB
   log_archive_gzip   1 开启 gzip 归档                    XQWUI_LOG_ARCHIVE_GZIP
   log_rotate_marker  1 新文件首行写切割标记
   log_level          日志级别（空 = 跟随 XQWUI_LOG_LEVEL，默认 INFO）
"""
import gzip
import logging
import logging.handlers
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta

from xqwui import config as appcfg

_FMT = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
_DATEFMT = "%m-%d %H:%M:%S"

CURRENT_NAME = "xqwui.log"          # 当前写入文件（路径恒定，不参与滚动改名）
_ROT_RE = re.compile(r"^xqwui-\d{8}-\d{6}(?:-\d+)?\.log(\.gz)?$")
WHENS = ("off", "hourly", "midnight")
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# config.json 缺失或值非法时的兜底（与 config.DEFAULTS 保持一致）
_FALLBACK = {
    "log_rotate_max_mb": 20.0,
    "log_rotate_when": "midnight",
    "log_keep_files": 10,
    "log_max_total_mb": 200.0,
    "log_archive_gzip": 1,
    "log_rotate_marker": 1,
    "log_level": "",
}
_RETRY_SEC = 30                     # 切割失败（文件被占用）后的重试退避秒数


def log_config() -> dict:
    """当前生效的日志配置：config.json 为主，环境变量可覆盖。

    每次调用重新读取，因此改配置后无需重启即可生效（见 reconfigure()）。
    """
    c = appcfg.load()

    def _num(key, cast, default):
        raw = os.environ.get("XQWUI_" + key.upper())
        if raw is None or str(raw).strip() == "":
            raw = c.get(key)
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return default

    def _flag(key):
        raw = os.environ.get("XQWUI_" + key.upper())
        if raw is None or str(raw).strip() == "":
            raw = c.get(key)
        try:
            return bool(int(raw))
        except (TypeError, ValueError):
            return bool(_FALLBACK.get(key))

    when = str(os.environ.get("XQWUI_LOG_ROTATE_WHEN")
               or c.get("log_rotate_when")
               or _FALLBACK["log_rotate_when"]).strip().lower()
    level = str(os.environ.get("XQWUI_LOG_LEVEL")
                or c.get("log_level")
                or "").strip().upper()
    return {
        "max_mb": max(0.0, _num("log_rotate_max_mb", float,
                                _FALLBACK["log_rotate_max_mb"])),
        "when": when if when in WHENS else _FALLBACK["log_rotate_when"],
        "keep_files": max(0, _num("log_keep_files", int,
                                  _FALLBACK["log_keep_files"])),
        "max_total_mb": max(0.0, _num("log_max_total_mb", float,
                                      _FALLBACK["log_max_total_mb"])),
        "gzip": _flag("log_archive_gzip"),
        "marker": _flag("log_rotate_marker"),
        "level": level if level in LEVELS else str(
            _FALLBACK["log_level"] or "INFO").upper(),
    }


def _hsize(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.1f %s" if unit != "B" else "%.0f %s") % (n, unit)
        n /= 1024.0
    return "-"


class _RotateHandler(logging.handlers.BaseRotatingHandler):
    """大小 + 时间双触发的滚动文件 handler。

    切割（doRollover）由 BaseRotatingHandler.emit 在 handler 的线程锁内
    调用，因此多线程下不会与写入交错；rename 在同一目录内原子完成。
    """

    def __init__(self, log_dir: str, encoding: str = "utf-8"):
        self.log_dir = log_dir
        self.path = os.path.join(log_dir, CURRENT_NAME)
        self.reason = "-"             # 本次切割触发原因（size/time/manual）
        self.last_rotated = ""        # 最近一次切割出的文件名
        self._next_at = 0.0           # 下次时间切割时刻（0 = 不按时间切割）
        self._retry_at = 0.0          # 切割失败后的重试时刻
        self._arch = set()            # 正在压缩的文件（去重）
        self._arch_lock = threading.Lock()
        os.makedirs(log_dir, exist_ok=True)
        logging.handlers.BaseRotatingHandler.__init__(
            self, self.path, "a", encoding, delay=False)
        self._next_at = self._next_boundary()

    # ------------------------------------------------------------ 配置 / 调度
    @property
    def cfg(self) -> dict:
        return log_config()

    def _next_boundary(self) -> float:
        """下一个时间切割时刻（when=off 返回 0）。"""
        when = self.cfg["when"]
        if when == "off":
            return 0.0
        now = datetime.now()
        nxt = (now + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0) if when == "hourly" \
            else (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
        return nxt.timestamp()

    # ------------------------------------------------------------ 切割判定
    def shouldRollover(self, record) -> bool:
        now = time.time()
        if self._next_at and now >= self._next_at:
            self.reason = "time"
            return True
        if self._retry_at and now < self._retry_at:
            return False                      # 上次切割失败，退避期内不再尝试
        max_bytes = self.cfg["max_mb"] * 1024 * 1024
        if max_bytes > 0:
            try:
                pos = self.stream.tell() if self.stream \
                    else os.path.getsize(self.path)
            except OSError:
                pos = 0
            if pos >= max_bytes:
                self.reason = "size"
                return True
        return False

    def doRollover(self):
        reason, self.reason = self.reason, "-"
        cfg = self.cfg
        if self.stream:                       # 1) 收尾：刷盘并关闭
            try:
                self.stream.flush()
            except (OSError, ValueError):
                pass
            self.stream.close()
            self.stream = None
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = 0

        dst = ""
        if size > 0:                          # 2) 改名：同目录原子 rename
            dst = self._free_name()
            try:
                os.rename(self.path, dst)
            except OSError as e:
                dst = ""
                self._retry_at = time.time() + _RETRY_SEC
                sys.stderr.write(
                    "[logutil] 日志切割失败（文件被占用），%ds 后重试: %s\n"
                    % (_RETRY_SEC, e))
        self.stream = self._open()            # 3) 立即 reopen，写入不中断
        self._next_at = self._next_boundary()
        self._retry_at = 0.0
        if not dst:
            return
        self.last_rotated = os.path.basename(dst)
        self._write_marker(reason, os.path.basename(dst), size)
        if cfg["gzip"]:                       # 4) 后台压缩，不阻塞写入
            self._archive_async(dst)
        self.prune()                          # 5) 按保留策略清理

    def _free_name(self) -> str:
        """历史文件名；同秒冲突追加 -2 / -3。"""
        ts = time.strftime("%Y%m%d-%H%M%S")
        name = "xqwui-%s.log" % ts
        i = 2
        while os.path.exists(os.path.join(self.log_dir, name)) \
                or os.path.exists(os.path.join(self.log_dir, name + ".gz")):
            name = "xqwui-%s-%d.log" % (ts, i)
            i += 1
        return os.path.join(self.log_dir, name)

    def _write_marker(self, reason: str, name: str, size: int):
        """新文件首行写切割标记，便于人工确认切割点。"""
        if not self.cfg["marker"] or not self.stream:
            return
        try:
            self.stream.write("%s | 日志切割 触发=%s 上一文件=%s 大小=%s\n"
                              % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                 reason, name, _hsize(size)))
            self.stream.flush()
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------ 归档 / 清理
    def _archive_async(self, path: str):
        """后台 gzip 压缩历史文件（.log → .log.gz），不占用日志锁。"""
        with self._arch_lock:
            if path in self._arch:
                return
            self._arch.add(path)

        def _run():
            tmp = path + ".gz.tmp"
            try:
                with open(path, "rb") as fi, \
                        gzip.open(tmp, "wb", compresslevel=6) as fo:
                    shutil.copyfileobj(fi, fo, 1024 * 1024)
                os.replace(tmp, path + ".gz")
                os.remove(path)
                self.prune()                  # 压缩后体积变小，复检一次容量
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            finally:
                with self._arch_lock:
                    self._arch.discard(path)

        threading.Thread(target=_run, daemon=True,
                         name="log-archive").start()

    def _rotated(self) -> list:
        """历史文件清单 [(path, mtime, size)]，按时间由旧到新。"""
        out = []
        try:
            names = os.listdir(self.log_dir)
        except OSError:
            return out
        for n in names:
            if not _ROT_RE.match(n):
                continue
            p = os.path.join(self.log_dir, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append((p, st.st_mtime, st.st_size))
        out.sort(key=lambda x: (x[1], x[0]))
        return out

    def _dir_size(self) -> int:
        """logs 目录实际占用（含当前文件与未识别文件）。"""
        total = 0
        try:
            for n in os.listdir(self.log_dir):
                if n.endswith(".tmp"):        # 压缩中间态不计入占用
                    continue
                p = os.path.join(self.log_dir, n)
                if os.path.isfile(p):
                    try:
                        total += os.path.getsize(p)
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    def prune(self) -> dict:
        """按保留策略清理：先按份数，再按总容量；返回清理结果。

        仅删除匹配 xqwui-<时间戳>.log[.gz] 的历史文件，当前文件不动。
        """
        cfg = self.cfg
        keep = int(cfg["keep_files"] or 0)
        cap = float(cfg["max_total_mb"] or 0) * 1024 * 1024
        olds = self._rotated()
        victims = [p for p, _, _ in olds[:max(0, len(olds) - keep)]]
        if cap > 0:
            total = self._dir_size()
            for p, _, s in olds:
                if total <= cap:
                    break
                if p in victims:
                    continue
                victims.append(p)
                total -= s
        removed, freed = [], 0
        for p in victims:
            try:
                sz = os.path.getsize(p)
                os.remove(p)
                removed.append(os.path.basename(p))
                freed += sz
            except OSError:
                pass
        return {"removed": removed, "freed": freed}


# ------------------------------------------------------------ 装配 / 对外接口
_state = {"dir": "", "level": "INFO"}


def _log_dir() -> str:
    return _state["dir"] or appcfg.log_dir()


def _file_handler() -> "_RotateHandler | None":
    for h in logging.getLogger("xqwui").handlers:
        if isinstance(h, _RotateHandler):
            return h
    return None


def setup(log_dir: str, level: str = "") -> None:
    """装配 xqwui.* 根 logger（幂等，重复调用仅重设级别）。"""
    root = logging.getLogger("xqwui")
    _state["dir"] = log_dir
    if not root.handlers:
        os.makedirs(log_dir, exist_ok=True)
        fmt = logging.Formatter(_FMT, _DATEFMT)
        con = logging.StreamHandler()
        con.setFormatter(fmt)
        file_ = _RotateHandler(log_dir, encoding="utf-8")
        file_.setFormatter(fmt)
        root.addHandler(con)
        root.addHandler(file_)
        root.propagate = False
        root.setLevel(logging.DEBUG)
        file_.prune()                     # 启动时先按策略清理一次
    if level:
        _state["level"] = level
    apply_level(_state["level"])


def apply_level(level: str = "") -> str:
    """设置控制台与文件 handler 的级别，返回实际生效级别。"""
    lv = getattr(logging, str(level or log_config()["level"]).upper(),
                 logging.INFO)
    for h in logging.getLogger("xqwui").handlers:
        h.setLevel(lv)
    return logging.getLevelName(lv)


def reconfigure() -> dict:
    """配置热更新：重设级别、重算时间边界、立即按新策略清理。"""
    level = apply_level()
    h = _file_handler()
    pruned = {"removed": [], "freed": 0}
    if h:
        with h.lock:
            h._next_at = h._next_boundary()
            h._retry_at = 0.0
            pruned = h.prune()
    return {"level": level, "pruned": pruned}


def rotate_now(reason: str = "manual") -> str:
    """手动触发一次切割，返回新历史文件名（空 = 未切割）。"""
    h = _file_handler()
    if not h:
        return ""
    with h.lock:
        h.reason = reason
        try:
            h.doRollover()
        except Exception:
            return ""
    return h.last_rotated


def prune_now() -> dict:
    """立即按保留策略清理历史文件。"""
    h = _file_handler()
    if not h:
        return {"removed": [], "freed": 0}
    with h.lock:
        return h.prune()


def clear(include_current: bool = False) -> dict:
    """清空历史文件；include_current=True 时同时截断当前文件。"""
    d = _log_dir()
    removed, freed = [], 0
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    for n in names:
        if not _ROT_RE.match(n):
            continue
        p = os.path.join(d, n)
        try:
            sz = os.path.getsize(p)
            os.remove(p)
            removed.append(n)
            freed += sz
        except OSError:
            pass
    if include_current:
        h = _file_handler()
        if h:
            with h.lock:                      # 截断当前文件：必须持锁
                try:
                    h.flush()
                    os.truncate(h.path, 0)
                    h.stream.seek(0)          # 文件被截断，写指针需回到开头
                    removed.append(CURRENT_NAME)
                except OSError:
                    pass
    return {"removed": removed, "freed": freed}


def list_files() -> dict:
    """日志文件清单（新的在前）+ 占用统计 + 当前生效配置。"""
    d = _log_dir()
    cfg = log_config()
    items = []
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    for n in names:
        if n.endswith(".tmp"):                # 压缩中的临时文件不展示
            continue
        p = os.path.join(d, n)
        if not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        items.append({"file": n, "size": st.st_size,
                      "mtime_ms": int(st.st_mtime * 1000),
                      "current": n == CURRENT_NAME,
                      "gz": n.endswith(".gz")})
    items.sort(key=lambda x: x["file"], reverse=True)   # 名称定长时间戳 → 新在前
    items.sort(key=lambda x: not x["current"])          # 当前文件置顶（稳定排序）
    h = _file_handler()
    return {
        "dir": d,
        "config": cfg,
        "files": items,
        "total": sum(i["size"] for i in items),
        "next_rotate_ms": int((h._next_at or 0) * 1000) if h else 0,
    }


def read_tail(name: str, lines: int = 200, max_bytes: int = 512 * 1024) -> dict:
    """读取日志尾部内容（支持 .gz），返回行列表与截断标记。"""
    safe = os.path.basename(str(name or ""))
    p = os.path.join(_log_dir(), safe)
    if not os.path.isfile(p):
        raise OSError("日志文件不存在: %s" % safe)
    opener = gzip.open if p.endswith(".gz") else open
    with opener(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - max_bytes)
        f.seek(start)
        raw = f.read(max_bytes)
    txt = raw.decode("utf-8", "replace")
    if start > 0:                             # 丢弃可能被截断的首行
        i = txt.find("\n")
        txt = txt[i + 1:] if i >= 0 else ""
    out = txt.splitlines()
    n = max(1, min(int(lines or 200), 5000))
    return {"file": safe, "size": size, "lines": out[-n:],
            "shown": min(len(out), n), "total_lines": len(out),
            "truncated": start > 0 or len(out) > n}


def get(name: str) -> logging.Logger:
    """取 xqwui.<name> 子 logger。"""
    return logging.getLogger("xqwui." + name)
