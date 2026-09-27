"""queueman —— 引擎侧队列与历史（线程安全）。

条目结构（与 ComfyUI /queue、/history 返回格式对齐）：
  queue item = (number, prompt_id, prompt, extra_data, outputs_to_execute)
  history[prompt_id] = {prompt, outputs, status, meta}
"""
import threading
import itertools
import time


class QueueManager:
    def __init__(self, history_max: int = 256):
        self._lock = threading.RLock()
        self._number = itertools.count(1)
        self._pending: list = []
        self._running = None            # 当前执行条目
        self._history: dict = {}
        self._history_order: list = []
        self._history_max = history_max
        self._flags = {"unload_models": False, "free_memory": False}
        self._interrupt_prompt_id = None   # 定向中断目标（None=任意）
        self._interrupted = threading.Event()

    # ---- 提交

    def put(self, prompt_id: str, prompt: dict, extra_data: dict,
            outputs: list | None) -> tuple:
        with self._lock:
            item = (next(self._number), prompt_id, prompt,
                    dict(extra_data or {}), outputs)
            self._pending.append(item)
            return item

    # ---- 查询

    def get_queue(self) -> dict:
        with self._lock:
            running = [self._running] if self._running else []
            return {"queue_running": running,
                    "queue_pending": list(self._pending)}

    @property
    def running(self):
        with self._lock:
            return self._running

    @property
    def running_prompt_id(self):
        with self._lock:
            return self._running[1] if self._running else None

    # ---- 取任务（工作线程）

    def pop_next(self, timeout: float = 1.0):
        """阻塞取出下一条 pending；无则返回 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._pending:
                    self._running = self._pending.pop(0)
                    return self._running
            time.sleep(0.05)
        return None

    def finish_running(self):
        with self._lock:
            self._running = None
            self._flags = {"unload_models": False, "free_memory": False}

    # ---- 删除 / 清空

    def delete_pending(self, prompt_ids):
        with self._lock:
            self._pending = [it for it in self._pending
                             if it[1] not in set(prompt_ids)]

    def clear_pending(self):
        with self._lock:
            self._pending.clear()

    # ---- 中断

    def request_interrupt(self, prompt_id: str | None):
        with self._lock:
            self._interrupt_prompt_id = prompt_id
        self._interrupted.set()

    def should_interrupt(self, current_prompt_id: str) -> bool:
        if not self._interrupted.is_set():
            return False
        with self._lock:
            target = self._interrupt_prompt_id
        return target is None or target == current_prompt_id

    def clear_interrupt(self):
        self._interrupted.clear()
        with self._lock:
            self._interrupt_prompt_id = None

    # ---- 历史

    def put_history(self, prompt_id: str, prompt: dict, outputs: dict,
                    status: dict, extra_data: dict):
        with self._lock:
            self._history[prompt_id] = {
                "prompt": [0, prompt_id, prompt, dict(extra_data or {}), None],
                "outputs": outputs,
                "status": status,
                "meta": prompt_id,
            }
            if prompt_id not in self._history_order:
                self._history_order.append(prompt_id)
            while len(self._history_order) > self._history_max:
                old = self._history_order.pop(0)
                self._history.pop(old, None)

    def get_history(self, prompt_id: str | None = None) -> dict:
        with self._lock:
            if prompt_id:
                v = self._history.get(prompt_id)
                return {prompt_id: v} if v else {}
            return {pid: self._history[pid] for pid in self._history_order}

    def delete_history(self, prompt_ids):
        with self._lock:
            for pid in prompt_ids:
                self._history.pop(pid, None)
                if pid in self._history_order:
                    self._history_order.remove(pid)

    def clear_history(self):
        with self._lock:
            self._history.clear()
            self._history_order.clear()
