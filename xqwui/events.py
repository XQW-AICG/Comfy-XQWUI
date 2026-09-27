"""events —— 应用内事件总线。

调度器 / 系统监控产生事件 → 订阅者（API 层 WS 推送桥）转发前端。
带最近事件缓冲，新订阅者可补发，避免刷新页面丢状态。
"""
import threading
import time


class EventBus:
    def __init__(self, buffer_size: int = 200):
        self._lock = threading.Lock()
        self._subs = []              # fn(evt: dict)
        self._buffer = []            # 最近事件（倒序裁剪）
        self._cap = buffer_size

    def subscribe(self, fn, replay: bool = False):
        """注册订阅者；replay=True 时先补发缓冲事件。返回退订函数。"""
        with self._lock:
            self._subs.append(fn)
            replayed = list(self._buffer) if replay else []
        for evt in replayed:
            try:
                fn(evt)
            except Exception:
                pass

        def _unsub():
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)
        return _unsub

    def emit(self, etype: str, data):
        """发布事件（订阅者异常互不影响）。"""
        evt = {"type": etype, "data": data, "ts": round(time.time(), 3)}
        with self._lock:
            self._buffer.append(evt)
            if len(self._buffer) > self._cap:
                self._buffer = self._buffer[-self._cap:]
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(evt)
            except Exception:
                pass


bus = EventBus()         # 应用级单例
