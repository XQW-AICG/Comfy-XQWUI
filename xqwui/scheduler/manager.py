"""manager —— 任务调度器。

单工作线程串行执行任务（消费级显存下引擎一次只跑一个 prompt）：
  取 queued 任务 → 逐段构建工作流（xqwui.workflow.h3_video）→
  素材同步到引擎 → POST /prompt → 经 WS 事件跟踪到段完成 →
  产物入 results 桶（命名「项目名称-分镜编号.mp4」，多段成片为
  「项目名称.mp4」）→ 全段完成后成片（单段直接复用段产物，多段 ffmpeg
  拼接）→ done。

状态权威在磁盘（data/tasks/*.json）：worker 与 REST 控制操作
（pause/cancel/resume）各自读写，决策点一律重新加载最新状态。

暂停 / 取消以「段」为粒度：
  pause  ：当前段跑完即 paused（queued 直接 paused）
  cancel ：interrupt 当前 prompt → canceled
  resume ：paused → queued，已完成段跳过续跑
  retry  ：error/canceled/done → queued；默认仅重置 error 段，
           带 from_segment 时重置该段及之后全部段（含产物清空），
           支持段级续跑与已完成任务局部重做

失败分类（error_kind，见 task._KINDS）：提交/收集/合并各失败路径
按异常类型映射（EngineError 自带 kind / ValueError→config /
StorageError·OSError→storage / 其余→internal）。
"""
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time

from xqwui import config as appcfg
from xqwui import events
from xqwui import procinfo
from xqwui.scheduler import task as tmodel
from xqwui.engine_client import EngineClient, EngineError, EngineWatcher
from xqwui.storage import router as storage
from xqwui.workflow import h3_video, timeline as tl

LOG = logging.getLogger("xqwui.scheduler")

CLIENT_ID = "xqwui-scheduler"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
# 段间续写（分段提交下的外部 handoff）：下一段承接上一段的末尾音频秒数
HANDOFF_AUDIO_SEC = 1.5
# 接缝交叉淡化时长（秒）：消除分段生成拼接处的爆音 / 硬切
SEAM_FADE_SEC = 0.15


def _ffmpeg_exe() -> str:
    return shutil.which(FFMPEG) or ""


def _tmpdir(prefix: str) -> str:
    d = os.path.join(appcfg.DATA_DIR, "tmp")
    os.makedirs(d, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=d)


class SchedulerManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._worker = None
        self._watcher = None
        self._eq: "queue.Queue" = queue.Queue(maxsize=2000)   # 引擎事件 → worker
        self._engine = None               # base 变更时重建
        self._engine_base = ""
        self._connected = False
        self._stop = False
        self._t0: dict = {}               # task id → 开始执行时刻（耗时统计）
        self._wake_seq: list = []         # 待调度任务 id（FIFO，按提交/分镜顺序）
        self._last_mem_log = 0.0          # 上次内存日志时刻

    # ------------------------------------------------------------ 生命周期
    def start(self):
        appcfg.ensure_dirs()
        self._recover()
        self._stop = False
        self._worker = threading.Thread(target=self._run_loop,
                                        name="scheduler", daemon=True)
        self._worker.start()
        # WS 订阅必须与 /prompt 提交使用同一 clientId：ComfyUI 把
        # progress / executing / execution_* 事件定向发给提交方会话，
        # 只有 status 是广播 —— 两端不一致时任务完成事件永远收不到
        self._watcher = EngineWatcher(self._engine_base_of,
                                      self._on_engine_event,
                                      client_id=CLIENT_ID)
        self._watcher.start()

    def stop(self):
        self._stop = True
        self._wake.set()
        if self._watcher:
            self._watcher.stop()

    def _engine_base_of(self) -> str:
        return str(appcfg.load().get("engine_url") or "").rstrip("/")

    def engine(self) -> EngineClient:
        base = self._engine_base_of()
        with self._lock:
            if self._engine is None or base != self._engine_base:
                self._engine = EngineClient(base)
                self._engine_base = base
            return self._engine

    def engine_status(self) -> dict:
        """/api/system/status 用：HTTP 探测 + WS 连接状态。"""
        st = {"connected": self._connected, "url": self._engine_base_of(),
              "rss_mb": round(procinfo.rss_mb(), 1)}
        try:
            stats = self.engine().system_stats()
            dev = (stats.get("devices") or [{}])[0]
            st.update({"reachable": True,
                       "version": (stats.get("system") or {}).get(
                           "comfyui_version", ""),
                       "gpu": dev.get("name", ""),
                       "vram_total": dev.get("vram_total", 0),
                       "vram_free": dev.get("vram_free", 0),
                       "queue": self.engine().queue()})
        except EngineError as e:
            st.update({"reachable": False, "error": str(e)})
        return st

    def _recover(self):
        """启动恢复：崩溃遗留的运行态任务。

        运行中的段先查引擎 history：若 prompt 已完成（如 WS 事件丢失
        导致卡住、或工作台重启时引擎已跑完），保留 prompt_id 走回传，
        不再重复提交生成；引擎不可达时保守重提。"""
        pending = []
        for t in tmodel.list_all():
            if t.state == "canceling":
                t.state = "canceled"
            elif t.state in ("running", "pausing"):
                t.state = "queued"
                t.started_ms = 0        # 停机期间不在生成，恢复后重新计时
                for s in t.segments:
                    if s.state == "running" and s.prompt_id:
                        done = False
                        try:
                            done = s.prompt_id in (
                                self.engine().history(s.prompt_id) or {})
                        except EngineError:
                            pass            # 引擎不可达：保守重提
                        if done:
                            # 保留 prompt_id，段置回 pending → _run_segment
                            # 走恢复路径直接回传（不重新生成）
                            s.state = "pending"
                            LOG.info("task=%s op=recover seg=%d 引擎已完成 "
                                     "prompt=%s，保留待回传",
                                     t.id, s.index + 1, s.prompt_id)
                        else:
                            s.state, s.prompt_id = "pending", ""
                    elif s.state == "running":
                        s.state = "pending"
            else:
                continue
            tmodel.save(t)
            pending.append(t)
        # 恢复入队按创建时间升序（FIFO）
        pending.sort(key=lambda x: x.created_ms)
        with self._lock:
            for t in pending:
                self._wake_seq.append(t.id)
        self._wake.set()

    def _queue_wake(self, task_id: str):
        """登记待调度任务（事件驱动：替代 worker 周期全量扫描磁盘）。

        追加到 FIFO 尾部 —— 批量提交按分镜顺序依次登记，保证按顺序执行。"""
        with self._lock:
            if task_id not in self._wake_seq:
                self._wake_seq.append(task_id)
        self._wake.set()

    def _take_wake_task(self) -> tmodel.Task | None:
        """按 FIFO 顺序取出已登记且确为 queued 的任务（以磁盘为准）。"""
        with self._lock:
            while self._wake_seq:
                i = self._wake_seq.pop(0)
                t = tmodel.load(i)
                if t and t.state == "queued":
                    return t
        return None

    def _maybe_mem_log(self):
        """每 30 分钟 INFO 一条内存占用（超阈值随时 WARNING）。"""
        now = time.time()
        if now - self._last_mem_log < 1800:
            return
        self._last_mem_log = now
        rss = procinfo.rss_mb()
        if rss > 2048:
            LOG.warning("op=mem rss=%.0fMB（超过 2GB，请排查任务数量/素材大小）",
                        rss)
        else:
            LOG.info("op=mem rss=%.0fMB", rss)

    # ------------------------------------------------------------ 对外操作
    def _precheck_queue(self) -> str:
        """入队前资源预检：队列深度 + 磁盘余量。返回错误串，空 = 通过。

        单卡显存下引擎一次只跑一个 prompt：queued/running/pausing 之外
        的任务不占流水线，不计入深度。磁盘按盘符去重，避免同一块 NVMe
        的多个目录重复检查。"""
        cfg = appcfg.load()
        limit = int(cfg.get("queue_limit", 3) or 0)
        if limit > 0:
            active = sum(1 for t in tmodel.list_all()
                         if t.state in ("queued", "running", "pausing"))
            if active >= limit:
                return ("排队/运行任务已达上限 %d，请等待完成后再提交"
                        % limit)
        reserve = float(cfg.get("disk_reserve_gb", 5.0) or 0)
        if reserve > 0:
            low = self._low_disk(reserve)
            if low:
                return ("%s 剩余空间不足 %.0fGB，拒绝入队（请先清理再试）"
                        % (low, reserve))
        return ""

    @staticmethod
    def _low_disk(reserve_gb: float) -> str:
        """返回余量不足的盘符描述（空 = 全部达标）。

        检查范围：results 桶 / 引擎输入目录 / 素材三分类目录。"""
        need = reserve_gb * (1024 ** 3)
        dirs = [appcfg.bucket_root("results"), appcfg.engine_data_dir()]
        dirs += [appcfg.materials_root(k)
                 for k in ("images", "videos", "audios")]
        checked = set()
        for d in dirs:
            try:
                drv = os.path.splitdrive(os.path.realpath(d))[0].upper()
                if not drv or drv in checked:
                    continue
                checked.add(drv)
                # 盘符根必然存在：子目录可能尚未创建，避免 disk_usage 抛错
                free = shutil.disk_usage(drv + os.sep).free
                if free < need:
                    return "%s 盘（剩 %.1fGB）" % (drv, free / 1024 ** 3)
            except OSError:
                continue
        return ""

    def submit(self, config: dict, name: str = "", batch_id: str = "",
               precheck: bool = True) -> dict:
        config = tl.resolve_prompt_refs(config)   # @素材: 引用归并
        errs = tl.validate_segments(config)
        if errs:
            return {"ok": False, "error": "；".join(errs)}
        if precheck:
            err = self._precheck_queue()
            if err:
                LOG.warning("op=submit 拒绝 name=%s 原因=%s",
                            (name or "（未命名）")[:40], err)
                return {"ok": False, "error": err}
        t = tmodel.Task(name, config)
        t.batch_id = str(batch_id or "")
        # 记录提交时所属项目名（成品命名「项目名称-分镜编号.mp4」）
        try:
            from xqwui import projects as _projects
            t.project_name = _projects.current_name()
        except Exception:
            t.project_name = ""
        tmodel.save(t)
        LOG.info("task=%s op=create segs=%d name=%s project=%s", t.id,
                 len(t.segments), (name or "（未命名）")[:40],
                 (t.project_name or "（无项目）")[:30])
        events.bus.emit("task_updated", t.summary())
        self._queue_wake(t.id)
        return {"ok": True, "task": t.summary()}

    def get(self, task_id: str) -> dict | None:
        t = tmodel.load(task_id)
        return t.to_dict() if t else None

    def summaries(self) -> list:
        return [t.summary() for t in tmodel.list_all()]

    def pause(self, task_id: str) -> dict:
        t = tmodel.load(task_id)
        if not t or t.state not in tmodel.ACTIVE:
            LOG.warning("op=pause 拒绝 task=%s 原因=任务不存在或不在可暂停状态",
                        task_id)
            return {"ok": False, "error": "任务不存在或不在可暂停状态"}
        prev = t.state
        if t.state == "running":
            t.state = "pausing"           # 当前段跑完即停
        elif t.state == "queued":
            t.state = "paused"
        else:
            LOG.warning("op=pause 拒绝 task=%s 原因=状态 %s 无需暂停",
                        task_id, t.state)
            return {"ok": False,
                    "error": "状态 %s 无需暂停（将自然暂停）" % t.state}
        tmodel.save(t)
        LOG.info("task=%s op=pause from=%s to=%s", t.id, prev, t.state)
        events.bus.emit("task_updated", t.summary())
        self._wake.set()
        return {"ok": True, "task": t.summary()}

    def resume(self, task_id: str) -> dict:
        t = tmodel.load(task_id)
        if not t or t.state != "paused":
            LOG.warning("op=resume 拒绝 task=%s 原因=任务不存在或未暂停",
                        task_id)
            return {"ok": False, "error": "任务不存在或未暂停"}
        t.state = "queued"
        tmodel.save(t)
        LOG.info("task=%s op=resume from=paused", t.id)
        events.bus.emit("task_updated", t.summary())
        self._queue_wake(t.id)
        return {"ok": True, "task": t.summary()}

    def cancel(self, task_id: str) -> dict:
        t = tmodel.load(task_id)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        prev = t.state
        if t.state == "queued":
            t.state = "canceled"
        elif t.state in ("running", "pausing", "paused"):
            t.state = "canceling"
            try:
                pid = next((s.prompt_id for s in t.segments
                            if s.state == "running" and s.prompt_id), None)
                self.engine().interrupt(pid)
            except EngineError:
                pass                      # 引擎不可达：worker 兜底落状态
        else:
            LOG.warning("op=cancel 拒绝 task=%s 原因=状态 %s 不可取消",
                        task_id, t.state)
            return {"ok": False, "error": "状态 %s 不可取消" % t.state}
        tmodel.save(t)
        LOG.info("task=%s op=cancel from=%s to=%s", t.id, prev, t.state)
        events.bus.emit("task_updated", t.summary())
        self._wake.set()
        return {"ok": True, "task": t.summary()}

    def retry(self, task_id: str,
              from_segment: int | None = None) -> dict:
        """重跑：默认只重置 error 段；from_segment=N 时该段及之后全部段
        重置（清空产物与 prompt_id），实现段级续跑 / 局部重做。"""
        t = tmodel.load(task_id)
        if not t or t.state not in ("error", "canceled", "done"):
            LOG.warning("op=retry 拒绝 task=%s 原因=仅失败/已取消/已完成任务可重跑",
                        task_id)
            return {"ok": False, "error": "仅失败/已取消/已完成任务可重跑"}
        frm = 0
        if from_segment is not None:
            try:
                frm = max(0, int(from_segment))
            except (TypeError, ValueError):
                return {"ok": False, "error": "from_segment 必须是整数"}
            if frm >= len(t.segments):
                return {"ok": False,
                        "error": "起始分镜超出范围（共 %d 个分镜）" % len(t.segments)}
        reset = 0
        for s in t.segments:
            if from_segment is None:
                if s.state != "error":
                    continue
                s.state, s.error, s.error_kind = "pending", "", ""
                s.prompt_id = ""
                reset += 1
            elif s.index >= frm:
                s.state, s.error, s.error_kind = "pending", "", ""
                s.prompt_id, s.file = "", ""
                reset += 1
        prev = t.state
        t.state, t.error, t.error_kind = "queued", "", ""
        t.result = {}
        t.progress = t.done_count / max(1, len(t.segments))
        t.started_ms, t.finished_ms = 0, 0     # 重跑重新计算总耗时
        tmodel.save(t)
        LOG.info("task=%s op=retry from=%s to=queued frm=%s 重置段=%d",
                 t.id, prev, frm, reset)
        events.bus.emit("task_updated", t.summary())
        self._queue_wake(t.id)
        return {"ok": True, "task": t.summary()}

    def delete(self, task_id: str) -> dict:
        t = tmodel.load(task_id)
        if not t:
            LOG.debug("op=delete 忽略 task=%s 原因=任务不存在", task_id)
            return {"ok": False, "error": "任务不存在"}
        if t.state in ("running", "pausing", "canceling"):
            LOG.warning("op=delete 拒绝 task=%s 原因=状态 %s 运行中",
                        task_id, t.state)
            return {"ok": False, "error": "运行中的任务请先取消再删除"}
        prev = t.state
        if t.state == "queued":
            t.state = "canceled"          # 防 worker 竞态：先出队再删
            tmodel.save(t)
        ok = tmodel.delete(task_id)
        LOG.info("task=%s op=delete from=%s 结果=%s", task_id, prev, ok)
        return {"ok": ok}

    # ------------------------------------------------------------ 批量
    def submit_batch(self, items: list, name_prefix: str = "") -> dict:
        """批量提交：items 为 [{name?, config}]（API 层已完成种子策略展开）。

        整批预检队列上限（按批量总数计，失败整批拒绝，避免部分入队的
        半截批次）；逐项提交失败不阻断其余项。返回批次号与逐项结果。"""
        items = [it for it in (items or []) if isinstance(it, dict)]
        if not items:
            return {"ok": False, "error": "批量列表为空"}
        if len(items) > 50:
            return {"ok": False,
                    "error": "单次批量提交上限 50 个任务（当前 %d）"
                             % len(items)}
        limit = int(appcfg.load().get("queue_limit", 3) or 0)
        if limit > 0:
            active = sum(1 for t in tmodel.list_all()
                         if t.state in ("queued", "running", "pausing"))
            if active + len(items) > limit:
                return {"ok": False,
                        "error": "排队/运行任务 %d 个 + 批量 %d 个超过上限 "
                                 "%d，请调高「队列上限」或分批提交"
                                 % (active, len(items), limit)}
        bid = "B" + time.strftime("%Y%m%d_%H%M%S") + os.urandom(3).hex()
        results = []
        for i, it in enumerate(items):
            cfg = it.get("config") or {}
            name = str(it.get("name") or "").strip() \
                or ("%s #%d" % (name_prefix or "批量任务", i + 1))
            r = self.submit(cfg, name, batch_id=bid, precheck=False)
            results.append({"index": i, "ok": bool(r.get("ok")),
                            "error": r.get("error") or "",
                            "task": (r.get("task") or {}).get("id", "")})
        accepted = [x for x in results if x["ok"]]
        LOG.info("op=batch-submit bid=%s 接受=%d 拒绝=%d",
                 bid, len(accepted), len(results) - len(accepted))
        return {"ok": bool(accepted), "batch_id": bid,
                "accepted": len(accepted),
                "rejected": len(results) - len(accepted),
                "results": results}

    def batches(self) -> list:
        """批处理报告聚合：按 batch_id 分组的统计与产物清单（新在前）。"""
        groups: dict = {}
        for t in tmodel.list_all():
            if not t.batch_id:
                continue
            g = groups.setdefault(t.batch_id, {
                "batch_id": t.batch_id, "name": t.name, "total": 0,
                "states": {}, "merged": [], "created_ms": t.created_ms,
                "updated_ms": t.updated_ms})
            g["total"] += 1
            g["states"][t.state] = g["states"].get(t.state, 0) + 1
            g["created_ms"] = min(g["created_ms"], t.created_ms)
            g["updated_ms"] = max(g["updated_ms"], t.updated_ms)
            if (t.result or {}).get("merged"):
                g["merged"].append({"task": t.id, "name": t.name,
                                    "key": t.result["merged"]})
        out = sorted(groups.values(), key=lambda g: -g["created_ms"])
        return out

    def batch_detail(self, batch_id: str) -> dict | None:
        """单个批次全部任务摘要（新在前）。"""
        tasks = [t for t in tmodel.list_all() if t.batch_id == batch_id]
        if not tasks:
            return None
        tasks.sort(key=lambda t: t.created_ms)
        return {"batch_id": batch_id,
                "tasks": [t.summary() for t in tasks]}

    # ------------------------------------------------------------ 归档
    def archive(self, task_id: str) -> dict:
        t = tmodel.load(task_id)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        if t.state in ("running", "pausing", "canceling"):
            return {"ok": False, "error": "运行中的任务请先取消再归档"}
        ok = tmodel.archive(task_id)
        if ok:
            LOG.info("task=%s op=archive from=%s", task_id, t.state)
        return {"ok": ok, "error": "" if ok else "归档失败（文件移动被拒）"}

    def restore(self, task_id: str) -> dict:
        ok = tmodel.restore(task_id)
        if ok:
            LOG.info("task=%s op=restore", task_id)
            self._queue_wake(task_id)     # 排队态任务恢复后继续调度
        return {"ok": ok, "error": "" if ok else "已归档任务不存在"}

    def summaries_archived(self) -> list:
        return [t.summary() for t in tmodel.list_archived()]

    # ------------------------------------------------------------ worker
    @staticmethod
    def _fresh(t: tmodel.Task) -> tmodel.Task | None:
        """以磁盘为权威重新加载（任务被删除时返回 None）。"""
        return tmodel.load(t.id)

    def _run_loop(self):
        last_scan = time.time()
        while not self._stop:
            self._wake.wait(timeout=15.0)
            self._wake.clear()
            # 事件驱动：只加载明确入队的任务，避免每 2 秒全量读盘
            t = self._take_wake_task()
            if t is None and time.time() - last_scan >= 30:
                last_scan = time.time()   # 低频兜底扫描（容错外部改动）
                # 兜底也按 FIFO：取创建最早的 queued 任务
                queued = [x for x in tmodel.list_all() if x.state == "queued"]
                t = min(queued, key=lambda x: (x.created_ms, x.id)) \
                    if queued else None
            while not self._stop and t:
                try:
                    self._run_task(t)
                except Exception as e:    # 兜底：不让 worker 死掉
                    LOG.exception("op=internal-error task=%s 异常=%s",
                                  t.id, e)   # ERROR 级，附完整堆栈
                    cur = self._fresh(t)
                    if cur:
                        self._fail(cur, "调度器内部错误: %s" % e,
                                   self._kind_of(e))
                t = self._take_wake_task()
            self._maybe_mem_log()

    def _save_emit(self, t: tmodel.Task):
        tmodel.save(t)
        events.bus.emit("task_updated", t.summary())

    def _set_stage(self, t: tmodel.Task, stage: str, detail: str = "",
                   eta_s: int | None = None):
        """更新任务阶段与实时状态描述并即时推送（阶段粒度，非高频）。"""
        t.stage = stage
        t.detail = detail
        if eta_s is not None:
            t.eta_s = max(0, int(eta_s))
        self._save_emit(t)

    def _fail(self, t: tmodel.Task, msg: str, kind: str = "internal"):
        """任务失败：错误分类落盘（task/段 error_kind，见 task._KINDS）。"""
        t.state, t.error, t.error_kind = "error", msg[:2000], kind
        for s in t.segments:
            if s.state == "running":
                s.state, s.error, s.error_kind = "error", msg[:500], kind
        t.stage, t.detail, t.eta_s = "error", "已停止：可查看原因后重试", 0
        LOG.error("task=%s op=fail kind=%s 原因=%s", t.id, kind, msg[:300])
        self._t0.pop(t.id, None)          # 及时释放耗时登记
        self._save_emit(t)

    @staticmethod
    def _kind_of(e: Exception, default: str = "internal") -> str:
        """异常 → error_kind：EngineError 自带 kind，其余按类型映射。"""
        if isinstance(e, EngineError):
            return e.kind or "engine_5xx"
        if isinstance(e, ValueError):
            return "config"
        if isinstance(e, subprocess.TimeoutExpired):
            return default
        if isinstance(e, (storage.StorageError, OSError)):
            return "storage"
        return default

    def _run_task(self, t: tmodel.Task):
        t = self._fresh(t)
        if not t:
            return
        self._t0[t.id] = time.time()
        t.state, t.error = "running", ""
        LOG.info("task=%s op=start segs=%d", t.id, len(t.segments))
        self._save_emit(t)
        while not self._stop:
            t = self._fresh(t)
            if not t or t.state != "running":
                if t:
                    LOG.info("task=%s op=stop-watching state=%s seg_done=%d/%d",
                             t.id, t.state,
                             sum(1 for s in t.segments if s.state == "done"),
                             len(t.segments))
                return                    # 被 pause/cancel/删除
            idx = next((i for i, s in enumerate(t.segments)
                        if s.state in ("pending", "error")), None)
            if idx is None:
                break
            self._run_segment(t, idx)
        t = self._fresh(t)
        if t and t.state == "running":
            if all(s.state == "done" for s in t.segments):
                LOG.info("task=%s op=segments-done elapsed=%.1fs 段数=%d",
                         t.id, time.time() - self._t0.get(t.id, time.time()),
                         len(t.segments))
                self._merge(t)
            else:
                self._fail(t, "存在未完成分镜")

    # ------------------------------------------------------------ 段执行
    _tt_cache: dict = {}

    def _director_task_type(self, eng: EngineClient) -> str:
        """Director 节点 task_type 取值适配（按引擎 object_info 解析，带缓存）。

        自带引擎（engine/nodes/h3.py）combo 为裸键 ["r2v", …]；ComfyUI 插件版
        为「r2v — 参考主体生视频(Reference to Video)」完整标签。取值必须与
        引擎 combo 完全一致，否则 /prompt 校验直接 400。
        """
        key = "r2v"
        cached = SchedulerManager._tt_cache.get(eng.base)
        if cached is not None:
            return cached
        val = key
        if len(SchedulerManager._tt_cache) > 16:      # 有界：URL 变更场景有限
            SchedulerManager._tt_cache.clear()
        try:
            spec = (eng.object_info("MiniMaxH3Director")
                    .get("MiniMaxH3Director") or {})
            opts = (((spec.get("input") or {}).get("required") or {})
                    .get("task_type") or [None])[0]
            if isinstance(opts, list):
                if key not in opts:
                    for o in opts:
                        if isinstance(o, str) and o.split(" ")[0] == key:
                            val = o
                            break
        except Exception:
            pass                    # 探测失败退回裸键（自带引擎语义）
        SchedulerManager._tt_cache[eng.base] = val
        return val

    def _run_segment(self, t: tmodel.Task, idx: int):
        eng = self.engine()
        seg = t.segments[idx]
        resumed = bool(seg.prompt_id)     # 恢复场景：引擎已有完成记录（_recover）
        t0 = time.time()
        if not resumed:
            seg.state, seg.error, seg.prompt_id = "running", "", ""
            LOG.info("task=%s op=segment-submit seg=%d/%d", t.id, idx + 1,
                     len(t.segments))
            self._set_stage(t, "sync",
                            "分镜 %d/%d · 正在同步参考素材到引擎…" % (idx + 1,
                                                               len(t.segments)),
                            eta_s=15)
        else:
            seg.state, seg.error = "running", ""
            LOG.info("task=%s op=segment-resume seg=%d/%d prompt=%s",
                     t.id, idx + 1, len(t.segments), seg.prompt_id)
            self._set_stage(t, "collect",
                            "分镜 %d/%d · 引擎已有成品，正在回传视频并入库…"
                            % (idx + 1, len(t.segments)), eta_s=10)
        if not resumed:
            try:
                self._sync_materials(t)
                cfg = dict(t.config)
                cfg["director_task_type"] = self._director_task_type(eng)
                self._set_stage(t, "submit",
                                "分镜 %d/%d · 正在提交工作流…" % (idx + 1,
                                                                 len(t.segments)))
                graph = h3_video.build_segment(cfg, idx)
                r = eng.post_prompt(graph, CLIENT_ID)
            except (EngineError, ValueError, storage.StorageError) as e:
                self._fail(t, "分镜 %d 提交失败: %s" % (idx + 1, e),
                           self._kind_of(e))
                return
            t = self._fresh(t)
            if not t:
                return
            t.segments[idx].prompt_id = r["prompt_id"]
            self._set_stage(t, "sampling",
                            "分镜 %d/%d · 引擎生成中（首次运行需先加载模型，稍慢）…"
                            % (idx + 1, len(t.segments)))
        outcome = self._await_prompt(t, idx)
        if outcome != "success":
            return                        # error / pause / cancel 已落状态
        t = self._fresh(t)
        if not t:
            return
        seg = t.segments[idx]
        self._set_stage(t, "collect",
                        "分镜 %d/%d · 生成完毕，正在回传视频并入库…"
                        % (idx + 1, len(t.segments)), eta_s=10)
        try:
            seg.file = self._collect_output(t, idx)
        except (EngineError, storage.StorageError, OSError) as e:
            self._fail(t, "分镜 %d 产物收集失败: %s" % (idx + 1, e),
                       self._kind_of(e))
            return
        seg.state = "done"
        LOG.info("task=%s op=segment-done seg=%d/%d elapsed=%.1fs 产物=%s",
                 t.id, idx + 1, len(t.segments), time.time() - t0, seg.file)
        self._emit_handoff(t, idx)      # 末帧 / 末尾音频 → 下一段参考
        t.progress = t.done_count / max(1, len(t.segments))
        self._save_emit(t)

    def _await_prompt(self, t: tmodel.Task, idx: int) -> str:
        """消费引擎事件流直到本段终结。返回 success/error/pause/cancel。"""
        pid = t.segments[idx].prompt_id
        frac = 0.0
        last_save = 0.0
        last_recon = time.time()
        idle_deadline = time.time() + 3600 * 4
        while not self._stop:
            try:
                msg = self._eq.get(timeout=2.0)
            except queue.Empty:
                if time.time() > idle_deadline:
                    cur = self._fresh(t)
                    if cur:
                        self._fail(cur, "分镜 %d 执行超时" % (idx + 1),
                                   "internal")
                    return "error"
                # 对账兜底：WS 事件丢失（断线窗口 / 队列溢出）时，
                # 每 10s 查引擎 history 确认本 prompt 是否实际已完成
                if time.time() - last_recon >= 10:
                    last_recon = time.time()
                    try:
                        if pid in (self.engine().history(pid) or {}):
                            LOG.warning(
                                "task=%s op=reconcile seg=%d "
                                "WS 事件缺失，经引擎 history 确认已完成",
                                t.id, idx + 1)
                            return "success"
                    except EngineError:
                        pass            # 引擎不可达：继续等事件 / 超时兜底
                continue
            etype = msg.get("type")
            data = msg.get("data") or {}
            if etype == "progress" and data.get("prompt_id") == pid:
                mx = float(data.get("max") or 0)
                if mx > 0:
                    frac = max(frac, float(data.get("value") or 0) / mx)
                    cur = self._fresh(t)
                    if not cur:
                        return "cancel"
                    total = max(1, len(cur.segments))
                    cur.progress = min(0.999,
                                       cur.done_count / total + frac / total)
                    # 预计剩余时间：总进度线性外推（跨段稳定，避免段间跳变）
                    el = time.time() - self._t0.get(cur.id, time.time())
                    cur.eta_s = int(max(0, el * (1 - cur.progress)
                                        / max(cur.progress, 0.02)))
                    cur.detail = ("分镜 %d/%d · 引擎生成 %s/%s 步"
                                  % (idx + 1, total, data.get("value"),
                                     data.get("max")))
                    # 进度盘存限频（≤2 次/秒），降低与任务列表读取的写读竞态；
                    # WS 进度事件仍每帧实时下发
                    if time.time() - last_save >= 0.5:
                        last_save = time.time()
                        tmodel.save(cur)
                    if LOG.isEnabledFor(logging.DEBUG):   # 高频路径：仅 DEBUG
                        LOG.debug("task=%s op=progress seg=%d %s/%s "
                                  "总体=%.1f%% eta=%ds", cur.id, idx + 1,
                                  data.get("value"), data.get("max"),
                                  cur.progress * 100, cur.eta_s)
                    events.bus.emit("task_progress", {
                        "id": cur.id, "segment": idx,
                        "value": data.get("value"), "max": data.get("max"),
                        "progress": round(cur.progress, 4),
                        "stage": cur.stage, "detail": cur.detail,
                        "eta_s": cur.eta_s})
            elif etype == "execution_success" and data.get("prompt_id") == pid:
                return "success"
            elif etype == "execution_error" and data.get("prompt_id") == pid:
                err = str(data.get("exception_message") or "引擎执行错误")
                cur = self._fresh(t)
                if cur:
                    cur.segments[idx].state = "error"
                    cur.segments[idx].error = err[:500]
                    cur.segments[idx].error_kind = "engine_5xx"
                    self._fail(cur, "分镜 %d 执行失败: %s" % (idx + 1, err),
                               "engine_5xx")
                return "error"
            elif etype == "execution_interrupted" and \
                    data.get("prompt_id", pid) == pid:
                return self._on_interrupt(t, idx)
        return "stop"

    def _on_interrupt(self, t: tmodel.Task, idx: int) -> str:
        """中断来源分流：取消 / 暂停 / 外部（重提一次）。"""
        cur = self._fresh(t)
        if not cur:
            return "cancel"
        if cur.state == "canceling":
            cur.segments[idx].state = "error"
            cur.segments[idx].error = "已取消"
            cur.state, cur.error = "canceled", "用户取消"
            self._save_emit(cur)
            return "cancel"
        if cur.state == "pausing":
            cur.segments[idx].state = "pending"
            cur.segments[idx].prompt_id = ""
            cur.state = "paused"
            self._save_emit(cur)
            return "pause"
        # 外部中断（如引擎侧清队列）：重提本段一次
        cur.state = "running"
        self._save_emit(cur)
        self._run_segment(cur, idx)
        return "success" if cur.segments[idx].state == "done" else "error"

    # ------------------------------------------------------------ 素材同步
    def _sync_materials(self, t: tmodel.Task):
        """任务引用素材 materials 桶 → 引擎输入目录（进程内每任务一次）。"""
        names = set()
        file_of = {im.get("id"): im.get("file")
                   for im in (t.config.get("images") or []) if im.get("file")}
        for s in (t.config.get("segments") or []):
            names.update(file_of[i] for i in (s.get("images") or [])
                         if i in file_of)
            names.update(str(x) for x in (s.get("refVideos") or []) if x)
            names.update(str(x) for x in (s.get("refAudios") or []) if x)
            names.update(str(x) for x in (s.get("firstFrame") or []) if x)
            names.update(str(x) for x in (s.get("lastFrame") or []) if x)
        todo = sorted(n for n in names if n not in t.synced)
        if not todo:
            return
        LOG.info("task=%s op=sync 上传素材=%d 个", t.id, len(todo))
        if LOG.isEnabledFor(logging.DEBUG):
            LOG.debug("task=%s op=sync 明细=%s", t.id, todo)
        eng = self.engine()
        for name in todo:
            src = storage.path_of("materials", name)
            if not os.path.isfile(src):
                raise storage.StorageError("素材缺失: %s" % name)
            # 流式分块上传：不整读文件进内存（视频素材可达数百 MB）
            remote = eng.upload_from_file(name, src, overwrite=True)
            if remote != name:
                raise storage.StorageError(
                    "引擎保存名 %r 与素材名 %r 不一致" % (remote, name))
            t.synced.add(name)

    # ------------------------------------------------------------ 产物收集
    def _out_base(self, t: tmodel.Task) -> str:
        """成品目录前缀：取设置「文件名前缀」（默认 xqwui/video）。

        '/' 保留为子目录层级；各段清洗 Windows 非法字符与首尾点空格；
        清洗后为空回退默认值。文件名部分由「项目名称-分镜编号」规则生成，
        前缀仅作目录归档。"""
        prefix = str((t.config or {}).get("filename_prefix")
                     or "xqwui/video").strip()
        parts = [re.sub(r'[\\:*?"<>|]+', "_", p).strip().strip(".")
                 for p in prefix.split("/")]
        parts = [p for p in parts if p]
        return "/".join(parts) or "xqwui/video"

    def _proj_slug(self, t: tmodel.Task) -> str:
        """成品文件名的项目名部分：清洗 Windows 非法字符；空回退任务短id。"""
        name = re.sub(r'[\\/:*?"<>|]+', "_", str(t.project_name or ""))
        name = name.strip().strip(".")
        return name or ("task_" + t.id[-6:])

    def _result_key(self, t: tmodel.Task, stem: str) -> str:
        """results 桶 key：{前缀目录}/{stem}.mp4，同名已存在时追加任务短id
        去重（如同项目多个任务的同号分镜 / 批量任务），避免覆盖历史成品。"""
        key = "%s/%s.mp4" % (self._out_base(t), stem)
        if storage.exists("results", key):
            key = "%s/%s_%s.mp4" % (self._out_base(t), stem, t.id[-6:])
        return key

    def _collect_output(self, t: tmodel.Task, idx: int) -> str:
        """引擎历史 → 段视频 → results 桶；返回 key。

        命名规则：{项目名称}-{分镜编号:02d}.mp4（如 夏日广告-03.mp4）；
        前缀目录下按同名去重。"""
        pid = t.segments[idx].prompt_id
        entry = (self.engine().history(pid) or {}).get(pid) or {}
        outs = []
        for node_out in (entry.get("outputs") or {}).values():
            outs += node_out.get("videos") or node_out.get("images") or []
        if not outs:
            raise EngineError("历史中无视频输出")
        ref = outs[0]
        tmp = _tmpdir("seg_")
        try:
            dst = os.path.join(tmp, "seg.mp4")
            self.engine().download(ref.get("filename", ""),
                                   ref.get("subfolder", ""),
                                   ref.get("type", "output"), dst)
            key = self._result_key(
                t, "%s-%02d" % (self._proj_slug(t), idx + 1))
            storage.put_file("results", key, dst, "video/mp4")
            return key
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------ 段间续写
    # 分段独立提交时 Director 的 continuity 不生效（每个 prompt 只有 1 段），
    # 故在调度层做外部 handoff：段 N 跑完后抽取末帧与末尾音频入库，
    # 登记为段 N+1 的参考素材，让画面与声音承接上一段。
    def _handoff_on(self, t: tmodel.Task) -> bool:
        """多段且「自动首尾帧续写」未被手动关闭时启用。"""
        if len(t.segments) < 2:
            return False
        v = t.config.get("continuity")
        return True if v is None else bool(v)

    @staticmethod
    def _attach_ref(t: tmodel.Task, i: int, field: str, key: str):
        """把素材 key 置入第 i 段的参考字段首位（去重、遵守上限）。

        同时写 t.config（生成链路：素材同步 / 工作流构建都读配置）与
        t.segments（任务详情展示），并对 images 补 id→file 映射。
        图片参考不设上限；视频 / 音频 ≤9（与 timeline.MAX_REF 一致）。
        """
        cap = None if field == "images" else 9
        segs = list(t.config.get("segments") or [])
        if i < len(segs):
            s = dict(segs[i])
            arr = [x for x in (s.get(field) or []) if x != key]
            arr.insert(0, key)
            s[field] = arr[:cap] if cap else arr
            segs[i] = s
            t.config["segments"] = segs
        if i < len(t.segments):
            s2 = t.segments[i]
            arr = [x for x in (getattr(s2, field, None) or []) if x != key]
            arr.insert(0, key)
            setattr(s2, field, arr[:cap] if cap else arr)
        if field == "images":
            maps = list(t.config.get("images") or [])
            if not any(im.get("id") == key for im in maps):
                maps.append({"id": key, "file": key})
                t.config["images"] = maps

    @staticmethod
    def _last_frame(exe: str, src: str, dst: str) -> bool:
        """抽取视频最后一帧：先 sseof 靠近末尾 seek，再回退 -vf reverse。

        image2 输出单图必须带 -update 1，否则按序列图处理且不落文件。
        """
        for args in (["-sseof", "-0.1"], ["-sseof", "-0.5"], []):
            cmd = [exe, "-y", "-v", "error"] + args + ["-i", src]
            if not args:
                cmd += ["-vf", "reverse"]
            cmd += ["-update", "1", "-frames:v", "1", "-q:v", "2", dst]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=180)
            except (OSError, subprocess.SubprocessError):
                continue
            if r.returncode == 0 and os.path.isfile(dst) \
                    and os.path.getsize(dst) > 0:
                return True
        return False

    def _emit_handoff(self, t: tmodel.Task, idx: int):
        """抽取段 idx 的末帧 / 末尾音频 → 段 idx+1 的参考素材。

        失败只告警不阻断：handoff 是增强项，缺失时下一段按原参考素材生成。
        """
        if idx + 1 >= len(t.segments) or not self._handoff_on(t):
            return
        exe = _ffmpeg_exe()
        if not exe:
            LOG.warning("task=%s op=handoff 跳过（PATH 中无 ffmpeg）", t.id)
            return
        src = storage.path_of("results", t.segments[idx].file)
        if not src or not os.path.isfile(src):
            return
        tmp = _tmpdir("handoff_")
        try:
            png = os.path.join(tmp, "last.png")
            wav = os.path.join(tmp, "tail.wav")
            nxt = idx + 2                       # 承接目标段号（1 基）
            got = []
            r1 = self._last_frame(exe, src, png)
            if r1:
                k = "handoff_%s_s%d.png" % (t.id, nxt)
                storage.put_file("materials", k, png, "image/png")
                self._attach_ref(t, idx + 1, "images", k)
                got.append(k)
            r2 = subprocess.run(
                [exe, "-y", "-v", "error", "-sseof",
                 "-%.2f" % HANDOFF_AUDIO_SEC, "-i", src, "-vn", "-ac", "2",
                 "-ar", "32000", "-c:a", "pcm_s16le", wav],
                capture_output=True, text=True, timeout=120)
            if r2.returncode == 0 and os.path.isfile(wav):
                k = "handoff_%s_s%d.wav" % (t.id, nxt)
                storage.put_file("materials", k, wav, "audio/wav")
                self._attach_ref(t, idx + 1, "refAudios", k)
                got.append(k)
            if got:
                LOG.info("task=%s op=handoff seg=%d → seg=%d 素材=%s",
                         t.id, idx + 1, nxt, ",".join(got))
        except (OSError, subprocess.SubprocessError,
                storage.StorageError) as e:
            LOG.warning("task=%s op=handoff 失败（不阻断成片）: %s", t.id, e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------ 合并
    def _finish(self, t: tmodel.Task, key: str, keys: list, put: str = ""):
        """成片落定：入库（put 非空时）/ 写 result / 置 done 并推送。

        单段任务的成片 key 直接复用段产物，不另存文件 —— 否则同一任务会
        产出两个内容相同、名称不同的视频（成品库重复卡片 + 双倍磁盘占用）。
        """
        if put:
            storage.put_file("results", key, put, "video/mp4")
        t.result = {"merged": key, "segments": keys}
        t.state, t.progress = "done", 1.0
        t.stage, t.detail, t.eta_s = "done", "生成完成，成片已入成品库", 0
        LOG.info("task=%s op=done elapsed=%.1fs rss=%.0fMB 产物=%s",
                 t.id, time.time() - self._t0.pop(t.id, time.time()),
                 procinfo.rss_mb(), key)
        self._save_emit(t)
        events.bus.emit("task_done", t.summary())

    def _merge_crossfade(self, exe: str, paths: list, out: str,
                         fade: float = SEAM_FADE_SEC) -> bool:
        """多段合流：视频直拼（不重编码）+ 音频链式 acrossfade → 合成。

        分段独立生成的接缝处音频若直接拼接会有爆音 / 突变，这里对相邻段
        音频做 tri 型交叉淡化（总时长缩短 fade×(段数-1)）。任一步失败返回
        False，由调用方回退到常规 concat。
        """
        if len(paths) < 2:
            return False
        d = os.path.dirname(paths[0])
        vpath = os.path.join(d, "v.mp4")
        apath = os.path.join(d, "a.m4a")
        listf = os.path.join(d, "v.txt")
        with open(listf, "w", encoding="utf-8") as f:
            for p in paths:
                f.write("file '%s'\n" % p.replace("'", "'\\''"))
        r = subprocess.run(
            [exe, "-y", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", listf, "-an", "-c", "copy", vpath],
            capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not os.path.isfile(vpath):
            return False
        cmd = [exe, "-y", "-v", "error"]
        for p in paths:
            cmd += ["-i", p]
        parts = ["[0:a][1:a]acrossfade=d=%s:c1=tri:c2=tri[a1]" % fade]
        prev = "[a1]"
        for i in range(2, len(paths)):
            tag = "[a%d]" % i
            parts.append("%s[%d:a]acrossfade=d=%s:c1=tri:c2=tri%s"
                         % (prev, i, fade, tag))
            prev = tag
        cmd += ["-filter_complex", ";".join(parts), "-map", prev,
                "-c:a", "aac", "-b:a", "160k", apath]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not os.path.isfile(apath):
            return False
        r = subprocess.run(
            [exe, "-y", "-v", "error", "-i", vpath, "-i", apath,
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy",
             "-movflags", "+faststart", out],
            capture_output=True, text=True, timeout=1800)
        return r.returncode == 0 and os.path.isfile(out)

    def _merge(self, t: tmodel.Task):
        keys = [s.file for s in t.segments if s.file]
        if len(keys) < 1:
            self._fail(t, "没有可合并的分镜产物", "internal")
            return
        if len(keys) == 1:
            # 单段：成片即该段产物本身，无需 ffmpeg，也不复制第二份文件
            LOG.info("task=%s op=merge 单段任务直接复用段产物成片=%s",
                     t.id, keys[0])
            self._finish(t, keys[0], keys)
            return
        exe = _ffmpeg_exe()
        if not exe:
            self._fail(t, "合并需要 ffmpeg（PATH 中未找到）", "internal")
            return
        self._set_stage(t, "merge",
                        "全部 %d 个分镜生成完毕，正在合并成片（长视频需数分钟）…"
                        % len(keys), eta_s=max(10, len(keys) * 5))
        events.bus.emit("task_merging", t.summary())
        tmp = _tmpdir("merge_")
        try:
            paths = []
            for i, k in enumerate(keys):
                p = os.path.join(tmp, "p%02d.mp4" % i)
                storage.download_to("results", k, p)
                paths.append(p)
            out = os.path.join(tmp, "merged.mp4")
            if len(paths) == 1:
                shutil.copyfile(paths[0], out)
            elif self._merge_crossfade(exe, paths, out):
                LOG.info("task=%s op=merge 接缝交叉淡化完成 段数=%d fade=%.2fs",
                         t.id, len(paths), SEAM_FADE_SEC)
            else:
                LOG.warning("task=%s op=merge 交叉淡化不可用，回退常规拼接",
                            t.id)
                listf = os.path.join(tmp, "list.txt")
                with open(listf, "w", encoding="utf-8") as f:
                    for p in paths:
                        f.write("file '%s'\n" % p.replace("'", "'\\''"))
                r = subprocess.run(
                    [exe, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                     "-i", listf, "-c", "copy", out],
                    capture_output=True, text=True, timeout=1800)
                if r.returncode != 0 or not os.path.isfile(out):
                    # 编码参数不一致时回退重编码
                    r = subprocess.run(
                        [exe, "-y", "-v", "error", "-f", "concat",
                         "-safe", "0", "-i", listf, "-c:v", "libx264",
                         "-crf", "18", "-preset", "medium",
                         "-pix_fmt", "yuv420p", "-c:a", "aac",
                         "-b:a", "128k", out],
                        capture_output=True, text=True, timeout=3600)
                    if r.returncode != 0 or not os.path.isfile(out):
                        self._fail(t, "ffmpeg 合并失败: %s"
                                   % (r.stderr or "").strip()[:600],
                                   "internal")
                        return
                    LOG.warning("task=%s op=merge 回退重编码（concat 直接合并失败）",
                                t.id)
            # 多段成片命名为 {项目名称}.mp4（单段任务成片直接复用
            # 段产物「项目名称-分镜编号.mp4」）；同名去重
            self._finish(t, self._result_key(t, self._proj_slug(t)),
                         keys, put=out)
        except (storage.StorageError, OSError,
                subprocess.TimeoutExpired) as e:
            self._fail(t, "合并失败: %s" % e, self._kind_of(e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------ 引擎事件
    def _on_engine_event(self, msg: dict):
        if msg.get("type") == "engine_status":
            conn = bool((msg.get("data") or {}).get("connected"))
            if conn != self._connected:          # 仅状态翻转时记录，避免噪音
                if conn:
                    LOG.info("引擎已连接 url=%s", self._engine_base_of())
                else:
                    LOG.warning("引擎连接断开（自动重连中） url=%s",
                                self._engine_base_of())
            self._connected = conn
            events.bus.emit("engine_status", {"connected": self._connected})
            return
        try:
            self._eq.put_nowait(msg)  # worker 按需消费；满则丢弃最旧，防无界增长
        except queue.Full:
            try:
                self._eq.get_nowait()
            except queue.Empty:
                pass
            self._eq.put_nowait(msg)
            LOG.warning("op=event-overflow 引擎事件队列已满，已丢弃最旧事件")


# ---------------------------------------------------------------- 单例
_manager: SchedulerManager | None = None
_mlock = threading.Lock()


def get_manager() -> SchedulerManager:
    global _manager
    with _mlock:
        if _manager is None:
            _manager = SchedulerManager()
        return _manager
