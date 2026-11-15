"""Periodic task descriptions and their registry (filled by the modules)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.scheduler.schedule import Schedule

#: ``slot -> payload`` of the published message (JSON-serializable).
PayloadFactory = Callable[[datetime], Mapping[str, Any]]


def task_queue(name: str) -> str:
    """Durable RabbitMQ queue of a task: ``tasks.<name>``."""
    return f"tasks.{name}"


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    """A periodic task: the leader of a slot publishes one message into ``queue``.

    ``catch_up=True``: on start the slot already passed (``previous_fire(start)``) is
    published too (same lock key - at most once per slot across replicas).
    """

    name: str
    schedule: Schedule
    queue: str
    payload_factory: PayloadFactory | None = None
    catch_up: bool = False

    def payload(self, slot: datetime) -> dict[str, Any]:
        return dict(self.payload_factory(slot)) if self.payload_factory is not None else {}


class TaskRegistry:
    """Registered tasks by unique name."""

    def __init__(self) -> None:
        self._tasks: dict[str, ScheduledTask] = {}

    def register(self, task: ScheduledTask) -> ScheduledTask:
        if task.name in self._tasks:
            raise ValueError(f"scheduled task {task.name!r} is already registered")
        self._tasks[task.name] = task
        return task

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tasks)

    def __iter__(self) -> Iterator[ScheduledTask]:
        return iter(tuple(self._tasks.values()))

    def __len__(self) -> int:
        return len(self._tasks)
