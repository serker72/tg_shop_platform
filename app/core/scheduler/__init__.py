"""Periodic tasks: schedule, registry, Redis slot lock, publication to RabbitMQ.

Only the mechanism lives here; tasks and their subscribers are registered by modules
(e.g. ``app/modules/bot_manager/tasks.py``). Runs in ``bot_worker`` only.
"""

from app.core.scheduler.lock import LOCK_KEY_PREFIX, SlotLock, lock_key
from app.core.scheduler.registry import ScheduledTask, TaskRegistry, task_queue
from app.core.scheduler.runner import (
    RESULTS,
    SCHEDULER_HEARTBEAT,
    RabbitTaskPublisher,
    SchedulerCollector,
    SchedulerRunner,
    SchedulerStats,
    TaskPublisher,
)
from app.core.scheduler.schedule import Schedule, parse_hhmm, slot_label

__all__ = [
    "LOCK_KEY_PREFIX",
    "RESULTS",
    "SCHEDULER_HEARTBEAT",
    "RabbitTaskPublisher",
    "Schedule",
    "ScheduledTask",
    "SchedulerCollector",
    "SchedulerRunner",
    "SchedulerStats",
    "SlotLock",
    "TaskPublisher",
    "TaskRegistry",
    "lock_key",
    "parse_hhmm",
    "slot_label",
    "task_queue",
]
