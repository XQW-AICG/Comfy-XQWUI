"""scheduler —— 任务调度子系统。

task     任务状态机与持久化（data/tasks/*.json）
manager  单线程调度器：分段提交引擎、事件跟踪、合并成片
"""
from xqwui.scheduler.manager import SchedulerManager, get_manager
from xqwui.scheduler import task

__all__ = ["SchedulerManager", "get_manager", "task"]
