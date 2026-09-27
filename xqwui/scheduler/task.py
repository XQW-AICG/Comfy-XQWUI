"""task —— 任务数据结构与持久化（data/tasks/<id>.json）。

状态机：
  queued ──▶ running ──▶ done
     │          │ ├────────▶ error
     │          └─pausing ─▶ paused ─（resume）─▶ queued
     └─（直接暂停/取消）      └─canceling ─▶ canceled
  error/canceled ─（retry）─▶ queued（保留已完成段，续跑）

段状态：pending → running → done / error（retry 时 error 段重置 pending）
"""
import os
import threading
import time

from shared.util import atomic_write_json, read_json
from xqwui import config as cfg

STATES = ("queued", "running", "pausing", "paused",
          "canceling", "canceled", "done", "error")
ACTIVE = ("queued", "running", "pausing", "canceling")
SEG_STATES = ("pending", "running", "done", "error")

_lock = threading.Lock()


def new_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S_") + os.urandom(3).hex()


class Segment:
    def __init__(self, index: int, prompt: str = ""):
        self.index = index
        self.prompt = prompt
        self.state = "pending"       # pending/running/done/error
        self.prompt_id = ""          # 引擎 prompt_id
        self.file = ""               # 段视频在 results 桶的 key
        self.error = ""
        self.error_kind = ""         # 错误类别（见 _KINDS）
        self.frames = 0              # snap 后帧数（生成时回填）

    def to_dict(self) -> dict:
        return {"index": self.index, "prompt": self.prompt,
                "state": self.state, "prompt_id": self.prompt_id,
                "file": self.file, "error": self.error,
                "error_kind": self.error_kind,
                "frames": self.frames}

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        s = cls(int(d.get("index", 0)), str(d.get("prompt") or ""))
        s.state = d.get("state") if d.get("state") in SEG_STATES else "pending"
        s.prompt_id = str(d.get("prompt_id") or "")
        s.file = str(d.get("file") or "")
        s.error = str(d.get("error") or "")
        s.error_kind = str(d.get("error_kind") or "")
        s.frames = int(d.get("frames") or 0)
        return s


class Task:
    def __init__(self, name: str, config: dict):
        self.id = new_id()
        self.name = name or ("任务 " + self.id)
        self.state = "queued"
        self.config = config or {}
        self.segments = [Segment(i, str(s.get("prompt") or ""))
                         for i, s in enumerate(self.config.get("segments") or [])]
        self.error = ""
        self.error_kind = ""         # 错误类别（见模块末尾 _KINDS 说明）
        self.progress = 0.0          # 0..1（done 段占比 + 当前段内部进度）
        self.stage = ""              # 当前阶段标识（sync/submit/sampling/…）
        self.detail = ""             # 实时状态描述（展示用）
        self.eta_s = 0               # 预计剩余秒数（线性外推）
        self.result = {}             # {"merged": key, "segments": [key...]}
        self.batch_id = ""           # 批量提交批次号（批处理报告聚合）
        self.project_name = ""       # 提交时所属项目名（成品命名 项目名-分镜号.mp4）
        self.created_ms = int(time.time() * 1000)
        self.updated_ms = self.created_ms
        self.started_ms = 0          # 首次进入 running 的时刻（总耗时起点）
        self.finished_ms = 0         # 进入终态（done/error/canceled）的时刻
        self.synced = set()          # 进程内：已同步到引擎的素材名（不持久化）

    # ------------------------------------------------------------ 序列化
    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "state": self.state,
                "error": self.error, "error_kind": self.error_kind,
                "progress": round(self.progress, 4),
                "stage": self.stage, "detail": self.detail,
                "eta_s": self.eta_s,
                "segments": [s.to_dict() for s in self.segments],
                "result": self.result, "batch_id": self.batch_id,
                "project_name": self.project_name,
                "created_ms": self.created_ms,
                "updated_ms": self.updated_ms,
                "started_ms": self.started_ms,
                "finished_ms": self.finished_ms}

    def summary(self) -> dict:
        """前端列表 / 推送用的轻量快照（不含完整配置）。"""
        d = self.to_dict()
        d["config"] = {
            "width": self.config.get("width"),
            "height": self.config.get("height"),
            "fps": self.config.get("fps"),
            "globalPrompt": self.config.get("globalPrompt", ""),
            "segmentCount": len(self.segments)}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        cfg_ = d.get("config") or {}
        t = cls(str(d.get("name") or ""), cfg_)
        t.id = str(d.get("id") or t.id)
        t.state = d.get("state") if d.get("state") in STATES else "error"
        t.error = str(d.get("error") or "")
        t.error_kind = str(d.get("error_kind") or "")
        t.progress = float(d.get("progress") or 0)
        t.stage = str(d.get("stage") or "")
        t.detail = str(d.get("detail") or "")
        t.eta_s = int(d.get("eta_s") or 0)
        t.result = dict(d.get("result") or {})
        t.batch_id = str(d.get("batch_id") or "")
        t.project_name = str(d.get("project_name") or "")
        t.created_ms = int(d.get("created_ms") or t.created_ms)
        t.updated_ms = int(d.get("updated_ms") or t.updated_ms)
        t.started_ms = int(d.get("started_ms") or 0)
        t.finished_ms = int(d.get("finished_ms") or 0)
        segs = d.get("segments")
        if isinstance(segs, list) and segs:
            t.segments = [Segment.from_dict(s) for s in segs]
        return t

    def seg(self, index: int) -> Segment:
        return self.segments[index]

    @property
    def done_count(self) -> int:
        return sum(1 for s in self.segments if s.state == "done")


# ------------------------------------------------------------ 持久化
def _safe_id(task_id: str) -> str:
    return "".join(c for c in task_id if c.isalnum() or c in "_-")


def _path(task_id: str, archived: bool = False) -> str:
    base = cfg.archive_dir() if archived else cfg.tasks_dir()
    return os.path.join(base, _safe_id(task_id) + ".json")


def _persist(task: Task):
    """config 全量 + 运行态落盘（崩溃后可恢复续跑）。"""
    os.makedirs(cfg.tasks_dir(), exist_ok=True)
    atomic_write_json(_path(task.id), {
        "task": task.to_dict(),
        "config": task.config,
    })


def save(task: Task) -> Task:
    now = int(time.time() * 1000)
    task.updated_ms = now
    # 总耗时登记（覆盖所有状态迁移路径）：首次进入 running 记起点，
    # 首次进入终态记终点；retry / 崩溃恢复会重置起点重新计时
    if task.state == "running" and not task.started_ms:
        task.started_ms = now
    if task.state in ("done", "error", "canceled") and not task.finished_ms:
        task.finished_ms = now
    with _lock:
        _persist(task)
    return task


def load(task_id: str, archived: bool = False) -> Task | None:
    d = read_json(_path(task_id, archived), None)
    if not d:
        return None
    t = Task.from_dict(d.get("task") or {})
    t.config = d.get("config") or t.config
    return t


def list_all() -> list:
    """全部任务（按创建时间倒序）。"""
    out = []
    d = cfg.tasks_dir()
    if os.path.isdir(d):
        for fn in os.listdir(d):
            if fn.endswith(".json"):
                t = load(fn[:-5])
                if t:
                    out.append(t)
    out.sort(key=lambda t: -t.created_ms)
    return out


def delete(task_id: str) -> bool:
    with _lock:
        try:
            os.remove(_path(task_id))
            return True
        except OSError:
            return False


# ------------------------------------------------------------ 归档
# 已归档任务移入 data/tasks/archive/：默认列表不可见，可恢复。
# 原子移动（os.replace）避免跨设备复制遗留。
def archive(task_id: str) -> bool:
    with _lock:
        src = _path(task_id)
        if not os.path.isfile(src):
            return False
        os.makedirs(cfg.archive_dir(), exist_ok=True)
        try:
            os.replace(src, _path(task_id, archived=True))
            return True
        except OSError:
            return False


def restore(task_id: str) -> bool:
    with _lock:
        src = _path(task_id, archived=True)
        if not os.path.isfile(src):
            return False
        os.makedirs(cfg.tasks_dir(), exist_ok=True)
        try:
            os.replace(src, _path(task_id))
            return True
        except OSError:
            return False


def list_archived() -> list:
    """已归档任务（按创建时间倒序）。"""
    out = []
    d = cfg.archive_dir()
    if os.path.isdir(d):
        for fn in os.listdir(d):
            if fn.endswith(".json"):
                t = load(fn[:-5], archived=True)
                if t:
                    out.append(t)
    out.sort(key=lambda t: -t.created_ms)
    return out


# ------------------------------------------------------------ 错误类别
# error_kind 取值（EngineError.kind / 调度器落值 / 前端徽标文案同源）：
#   engine_4xx        引擎拒绝请求（配置/模型/素材校验问题）—— 先改配置
#   engine_5xx        引擎内部错误 —— 可原样重试
#   engine_unreachable 引擎离线 / 地址不可达
#   network           连接层失败
#   config            本地配置不合法（工作流构建期）
#   storage           存储读写失败
#   disk              磁盘余量不足（入队预检）
#   queue             队列深度超限（入队预检）
#   internal          调度器内部错误
_KINDS = ("engine_4xx", "engine_5xx", "engine_unreachable", "network",
          "config", "storage", "disk", "queue", "internal")
