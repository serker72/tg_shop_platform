"""``SchedulerRunner``: tick -> slot lock -> publish to RabbitMQ -> metrics.

Guarantees (handlers MUST be idempotent):

* at most one *successful* publication per slot across replicas (lock on the planned time);
* a failed or cancelled publication drops the lock (owner check) - the next tick retries;
* a publication whose confirm timed out may still have reached the queue: the retry then
  enqueues a duplicate (same ``message_id``) - at-least-once on that path;
* an ambiguous ``SET NX`` (written by Redis, reply lost) leaves the slot locked by nobody
  alive: every replica sees ``skipped`` until the TTL - at-most-once, the slot is lost;
* a Redis error fails closed (no publication, ``result="error"``).
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import structlog
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector

from app.core.observability.health import publish_heartbeat
from app.core.scheduler.lock import SlotLock
from app.core.scheduler.registry import ScheduledTask, TaskRegistry
from app.core.scheduler.schedule import slot_label

if TYPE_CHECKING:
    from faststream.rabbit import RabbitBroker

RunResult = Literal["published", "skipped", "error"]
RESULTS: Final[tuple[RunResult, ...]] = ("published", "skipped", "error")
#: Non-critical heartbeat component (``heartbeat:scheduler`` in readiness).
SCHEDULER_HEARTBEAT: Final = "scheduler"
PUBLISH_TIMEOUT_SECONDS: Final = 10.0

_log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TaskPublisher(Protocol):
    async def publish(self, queue: str, message: dict[str, Any], *, message_id: str) -> None: ...


class RabbitTaskPublisher:
    """Publishes via a :func:`build_rabbit_broker` broker (OTel/trace_id middlewares).

    Durable queue name, ``persist=True`` (delivery_mode=2); publisher confirms are on by
    default in FastStream 0.7 (``Channel.publisher_confirms=True``): a nack raises.
    """

    def __init__(self, broker: RabbitBroker) -> None:
        self._broker = broker

    async def publish(self, queue: str, message: dict[str, Any], *, message_id: str) -> None:
        from faststream.rabbit import RabbitQueue

        await self._broker.publish(
            message,
            queue=RabbitQueue(queue, durable=True),
            persist=True,
            message_id=message_id,
            timeout=PUBLISH_TIMEOUT_SECONDS,
        )


class SchedulerStats:
    """In-process counters exported by :class:`SchedulerCollector`."""

    def __init__(self) -> None:
        self.runs: dict[tuple[str, str], int] = {}
        self.last_success: dict[str, float] = {}

    def inc(self, task: str, result: RunResult) -> None:
        self.runs[(task, result)] = self.runs.get((task, result), 0) + 1


class SchedulerCollector(Collector):
    """``scheduler_runs_total{task,result}`` (zeros for every known task/result) and
    ``scheduler_last_success_timestamp_seconds{task}`` (after the first publication)."""

    def __init__(self, stats: SchedulerStats, tasks: Iterable[str]) -> None:
        self._stats = stats
        self._tasks = tuple(tasks)

    def collect(self) -> Iterable[Metric]:
        runs = CounterMetricFamily(
            "scheduler_runs", "Scheduler slot attempts by result.", labels=["task", "result"]
        )
        values: dict[tuple[str, str], int] = {
            (task, result): 0 for task in self._tasks for result in RESULTS
        }
        values.update(self._stats.runs)
        for (task, result), value in sorted(values.items()):
            runs.add_metric([task, result], value)
        yield runs
        last = GaugeMetricFamily(
            "scheduler_last_success_timestamp_seconds",
            "Unix time of the last successful publication of a task.",
            labels=["task"],
        )
        for task, ts in sorted(self._stats.last_success.items()):
            last.add_metric([task], ts)
        yield last


class SchedulerRunner:
    """Ticks every ``tick_seconds``; one replica per slot wins the lock and publishes."""

    def __init__(
        self,
        registry: TaskRegistry,
        redis: Any,
        publisher: TaskPublisher,
        *,
        stats: SchedulerStats | None = None,
        tick_seconds: float = 30.0,
        clock: Callable[[], datetime] = _utcnow,
        heartbeat_ttl: int | None = None,
        instance: str | None = None,
    ) -> None:
        self._registry = registry
        self._redis = redis
        self._lock = SlotLock(redis)
        self._publisher = publisher
        self.stats = stats or SchedulerStats()
        self._tick = tick_seconds
        self._clock = clock
        self._heartbeat_ttl = heartbeat_ttl
        self._instance = instance or f"{socket.gethostname()}:{os.getpid()}"
        self._done: dict[str, datetime] = {}
        self.tasks: list[asyncio.Task[None]] = []
        for task in registry:
            if task.schedule.period_seconds < tick_seconds:
                # Slots shorter than the tick are skipped (only the latest one is published).
                _log.warning(
                    "scheduler_period_shorter_than_tick",
                    task=task.name,
                    period_seconds=task.schedule.period_seconds,
                    tick_seconds=tick_seconds,
                )

    @property
    def heartbeat_interval(self) -> float:
        """Beat every ``ttl / 3`` (>= 1 s): a single late beat never expires the heartbeat."""
        return max(1.0, (self._heartbeat_ttl or 3) / 3)

    def prime(self, now: datetime) -> None:
        """Start state: tasks without ``catch_up`` wait for the next slot after ``now``."""
        for task in self._registry:
            if not task.catch_up:
                self._done[task.name] = task.schedule.previous_fire(now)

    async def tick(self, now: datetime) -> dict[str, RunResult]:
        """Handle every task whose current slot is not done yet; returns the attempts."""
        results: dict[str, RunResult] = {}
        for task in self._registry:
            slot = task.schedule.previous_fire(now)
            if self._done.get(task.name) == slot:
                continue
            results[task.name] = await self._run(task, slot)
        return results

    async def _run(self, task: ScheduledTask, slot: datetime) -> RunResult:
        label = slot_label(slot)
        try:
            token = await self._lock.acquire(task.name, slot, task.schedule.lock_ttl_seconds)
        except Exception as error:  # RedisError/OSError (incl. OOM) and anything else
            # Fail closed: never assume another replica took the slot.
            _log.error(
                "scheduler_lock_failed",
                task=task.name,
                scheduled_at=label,
                error=type(error).__name__,
                detail=str(error),
            )
            return self._result(task.name, "error")
        if token is None:
            self._done[task.name] = slot
            _log.info("scheduler_slot_taken", task=task.name, scheduled_at=label)
            return self._result(task.name, "skipped")
        try:
            message = {"task": task.name, "scheduled_at": label, "payload": task.payload(slot)}
            await self._publisher.publish(task.queue, message, message_id=f"{task.name}:{label}")
        except BaseException as error:
            # Also on cancel (shutdown): free the slot so another replica / restart retries.
            released = await asyncio.shield(self._lock.release(task.name, slot, token))
            self._result(task.name, "error")
            if not isinstance(error, Exception):
                _log.warning(
                    "scheduler_publish_cancelled",
                    task=task.name,
                    scheduled_at=label,
                    released=released,
                )
                raise
            _log.exception(
                "scheduler_publish_failed", task=task.name, scheduled_at=label, released=released
            )
            return "error"
        self._done[task.name] = slot
        self.stats.last_success[task.name] = time.time()
        _log.info("scheduler_task_published", task=task.name, scheduled_at=label)
        return self._result(task.name, "published")

    def _result(self, task: str, result: RunResult) -> RunResult:
        self.stats.inc(task, result)
        return result

    async def run_forever(self) -> None:
        """Tick loop; no exception except cancellation ever leaves it."""
        self.prime(self._clock())
        while True:
            try:
                await self.tick(self._clock())
            except Exception:
                _log.exception("scheduler_tick_failed")
            await asyncio.sleep(self._tick)

    async def heartbeat_forever(self) -> None:
        """``heartbeat:scheduler`` every :attr:`heartbeat_interval`, independent of ticks."""
        if self._heartbeat_ttl is None:
            return
        while True:
            try:
                await publish_heartbeat(
                    self._redis, SCHEDULER_HEARTBEAT, self._instance, ttl=self._heartbeat_ttl
                )
            except Exception:
                _log.exception("scheduler_heartbeat_failed")
            await asyncio.sleep(self.heartbeat_interval)

    @asynccontextmanager
    async def running(self) -> AsyncIterator[list[asyncio.Task[None]]]:
        """Tick (and heartbeat) loops for the lifetime of the context; cancelled on exit.

        Yields the tasks: the caller hands them to ``ServiceContext.supervisor.watch``.
        """
        self.tasks = [asyncio.create_task(self.run_forever(), name="scheduler")]
        if self._heartbeat_ttl is not None:
            self.tasks.append(
                asyncio.create_task(self.heartbeat_forever(), name="scheduler-heartbeat")
            )
        try:
            yield self.tasks
        finally:
            for loop_task in self.tasks:
                loop_task.cancel()
            for loop_task in self.tasks:
                with suppress(asyncio.CancelledError):
                    await loop_task
