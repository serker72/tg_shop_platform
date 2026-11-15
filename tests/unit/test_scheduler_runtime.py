"""Scheduler runtime: cancellation, task errors, heartbeat loop, broker wiring, component."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from faststream.rabbit import RabbitBroker, TestRabbitBroker
from prometheus_client import CollectorRegistry
from redis.asyncio import Redis
from structlog.testing import capture_logs

from app.core.process import ServiceContext
from app.core.scheduler import Schedule, ScheduledTask, SchedulerRunner, TaskRegistry
from app.entrypoints.bot_worker import scheduler as component_mod
from app.modules.bot_manager import tasks as tasks_mod
from tests.unit.test_scheduler import FakeRedis, RecordingPublisher

pytestmark = pytest.mark.req("R-II.15")

NOW = datetime(2026, 11, 16, 0, 0, 1, tzinfo=UTC)


class _Pipeline:
    def __init__(self, beats: list[str]) -> None:
        self._beats = beats

    async def __aenter__(self) -> _Pipeline:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def zadd(self, key: str, mapping: Mapping[str, float]) -> None:
        self._beats.append(key)

    def zremrangebyscore(self, *args: Any) -> None:
        return None

    def expire(self, *args: Any) -> None:
        return None

    async def execute(self) -> list[Any]:
        return []


class BeatRedis(FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.beats: list[str] = []

    def pipeline(self, transaction: bool = True) -> _Pipeline:
        return _Pipeline(self.beats)


def _registry(*tasks: ScheduledTask) -> TaskRegistry:
    registry = TaskRegistry()
    for task in tasks or (ScheduledTask("t.daily", Schedule.daily_at("00:00"), "tasks.t.daily"),):
        registry.register(task)
    return registry


async def test_cancel_during_publish_releases_lock() -> None:
    redis = FakeRedis()
    started = asyncio.Event()

    class Hanging:
        async def publish(self, queue: str, message: dict[str, Any], *, message_id: str) -> None:
            started.set()
            await asyncio.sleep(3600)

    runner = SchedulerRunner(_registry(), redis, Hanging())
    tick = asyncio.create_task(runner.tick(NOW))
    await started.wait()
    assert redis.store
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick
    assert redis.store == {}
    assert runner.stats.runs == {("t.daily", "error"): 1}


async def test_payload_error_releases_lock_and_other_tasks_still_run() -> None:
    def broken(slot: datetime) -> Mapping[str, Any]:
        raise ValueError("bad payload")

    registry = _registry(
        ScheduledTask("a.broken", Schedule.daily_at("00:00"), "tasks.a", payload_factory=broken),
        ScheduledTask("b.ok", Schedule.daily_at("00:00"), "tasks.b"),
    )
    redis = FakeRedis()
    publisher = RecordingPublisher()
    runner = SchedulerRunner(registry, redis, publisher)
    assert await runner.tick(NOW) == {"a.broken": "error", "b.ok": "published"}
    assert list(redis.store) == ["sched:lock:b.ok:2026-11-16T00:00:00Z"]
    assert [queue for queue, _ in publisher.messages] == ["tasks.b"]


async def test_heartbeat_runs_on_own_interval_and_loops_survive_errors() -> None:
    redis = BeatRedis()
    redis.error = RuntimeError("unexpected")  # tick errors must not kill the loop
    runner = SchedulerRunner(_registry(), redis, RecordingPublisher(), tick_seconds=3600)
    runner_hb = SchedulerRunner(
        _registry(), redis, RecordingPublisher(), tick_seconds=3600, heartbeat_ttl=30
    )
    assert runner_hb.heartbeat_interval == 10
    assert runner.heartbeat_interval >= 1
    async with runner_hb.running() as loop_tasks:
        assert {t.get_name() for t in loop_tasks} == {"scheduler", "scheduler-heartbeat"}
        await asyncio.sleep(0.05)
        assert all(not t.done() for t in loop_tasks)
    assert redis.beats == ["heartbeat:scheduler"]
    assert all(t.cancelled() for t in loop_tasks)


def test_period_shorter_than_tick_warns() -> None:
    registry = _registry(ScheduledTask("fast", Schedule.every(10), "tasks.fast"))
    with capture_logs() as logs:
        SchedulerRunner(registry, FakeRedis(), RecordingPublisher(), tick_seconds=30)
    assert any(e["event"] == "scheduler_period_shorter_than_tick" for e in logs)


def _settings() -> Any:
    return SimpleNamespace(
        scheduler=SimpleNamespace(
            scheduler_enabled=True, scheduler_tick_seconds=3600, proxy_rehabilitate_at="03:00"
        ),
        observability=SimpleNamespace(heartbeat_ttl_seconds=30),
        proxy=SimpleNamespace(proxy_quarantine_days=7),
    )


async def test_rehabilitate_subscriber_via_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    async def fake(factory: Any, settings: Any, redis: Any, *, now: Any = None) -> int:
        calls.append((factory, settings, redis))
        return 3

    monkeypatch.setattr(tasks_mod, "rehabilitate_proxies", fake)
    broker = RabbitBroker()
    registry = TaskRegistry()
    settings = _settings()
    task = tasks_mod.register_proxy_tasks(
        registry, broker, settings=settings, session_factory=cast(Any, "sf"), redis="r"
    )
    assert task.queue == "tasks.proxy.rehabilitate"
    assert task.catch_up is True
    assert task.schedule == Schedule.daily_at("03:00")
    async with TestRabbitBroker(broker) as br:
        await br.publish(
            {"task": task.name, "scheduled_at": "2026-11-16T03:00:00Z", "payload": {}},
            queue=task.queue,
        )
    assert calls == [("sf", settings.proxy, "r")]


async def test_scheduler_component_enter_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(*args: Any, **kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(tasks_mod, "rehabilitate_proxies", fake)
    broker = RabbitBroker()
    monkeypatch.setattr(component_mod, "build_rabbit_broker", lambda settings: broker)
    redis = BeatRedis()

    class Container:
        async def get(self, kind: Any) -> Any:
            return redis if kind is Redis else "session-factory"

    metrics = SimpleNamespace(registry=CollectorRegistry())
    ctx = ServiceContext(
        name="bot_worker",
        settings=_settings(),
        container=cast(Any, Container()),
        metrics=cast(Any, metrics),
    )
    async with TestRabbitBroker(broker):
        async with component_mod.scheduler_component(ctx):
            await asyncio.sleep(0.05)
            # catch_up: the 03:00 slot already passed today -> published on start.
            assert any(k.startswith("sched:lock:proxy.rehabilitate:") for k in redis.store)
            assert redis.beats
            ctx.supervisor.begin_shutdown()  # as serve_components does before exit
    assert ctx.supervisor.failed == []
    assert (
        metrics.registry.get_sample_value(
            "scheduler_runs_total", {"task": "proxy.rehabilitate", "result": "published"}
        )
        == 1.0
    )
