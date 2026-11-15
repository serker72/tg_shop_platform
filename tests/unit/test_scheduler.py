"""Scheduler mechanism with a fake clock, an in-memory Redis and ``TestRabbitBroker``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from faststream.rabbit import RabbitBroker, RabbitQueue, TestRabbitBroker
from prometheus_client import CollectorRegistry, generate_latest
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from app.core.config import SchedulerSettings
from app.core.scheduler import (
    RabbitTaskPublisher,
    Schedule,
    ScheduledTask,
    SchedulerCollector,
    SchedulerRunner,
    TaskRegistry,
    lock_key,
)

pytestmark = pytest.mark.req("R-II.15")


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, tuple[str, int]] = {}
        self.error: Exception | None = None

    async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool | None:
        if self.error is not None:
            raise self.error
        if nx and key in self.store:
            return None
        self.store[key] = (value, ex)
        return True

    async def eval(self, script: str, numkeys: int, key: str, token: str) -> int:
        if key in self.store and self.store[key][0] == token:
            del self.store[key]
            return 1
        return 0


class RecordingPublisher:
    def __init__(self, failures: int = 0) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []
        self.failures = failures

    async def publish(self, queue: str, message: dict[str, Any], *, message_id: str) -> None:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("broker down")
        self.messages.append((queue, message))


def _registry(*, at: str = "00:00", catch_up: bool = False) -> TaskRegistry:
    registry = TaskRegistry()
    registry.register(
        ScheduledTask("t.daily", Schedule.daily_at(at), "tasks.t.daily", catch_up=catch_up)
    )
    return registry


def test_schedule_previous_fire_and_ttl() -> None:
    daily = Schedule.daily_at("03:00")
    assert daily.previous_fire(_at("2026-11-15T02:59:59.999")) == _at("2026-11-14T03:00:00")
    assert daily.previous_fire(_at("2026-11-15T03:00:00")) == _at("2026-11-15T03:00:00")
    assert daily.lock_ttl_seconds == 25 * 3600
    every = Schedule.every(300)
    assert every.previous_fire(_at("2026-11-15T10:07:31")) == _at("2026-11-15T10:05:00")
    assert every.lock_ttl_seconds >= 300
    with pytest.raises(ValueError, match="naive"):
        daily.previous_fire(datetime(2026, 11, 15))  # noqa: DTZ001
    with pytest.raises(ValueError, match="HH:MM"):
        Schedule.daily_at("24:00")


def test_lock_key_uses_planned_slot_utc() -> None:
    assert lock_key("proxy.rehabilitate", _at("2026-11-15T03:00:00")) == (
        "sched:lock:proxy.rehabilitate:2026-11-15T03:00:00Z"
    )


async def test_two_replicas_on_day_boundary_publish_once() -> None:
    broker = RabbitBroker()
    received: list[dict[str, Any]] = []

    @broker.subscriber(RabbitQueue("tasks.t.daily", durable=True))
    async def handle(body: dict[str, Any]) -> None:
        received.append(body)

    redis = FakeRedis()
    async with TestRabbitBroker(broker):
        replicas = [
            SchedulerRunner(_registry(), redis, RabbitTaskPublisher(broker)) for _ in range(2)
        ]
        for replica in replicas:
            replica.prime(_at("2026-11-15T23:59:00"))
        a, b = replicas
        assert await a.tick(_at("2026-11-15T23:59:59.9")) == {}
        assert await b.tick(_at("2026-11-16T00:00:00.1")) == {"t.daily": "published"}
        assert await a.tick(_at("2026-11-16T00:00:00.2")) == {"t.daily": "skipped"}
        assert await a.tick(_at("2026-11-16T00:00:30")) == {}
        assert await b.tick(_at("2026-11-16T00:00:30")) == {}
    assert len(received) == 1
    assert received[0]["scheduled_at"] == "2026-11-16T00:00:00Z"
    assert received[0]["task"] == "t.daily"
    key = "sched:lock:t.daily:2026-11-16T00:00:00Z"
    assert redis.store[key][1] == 25 * 3600


async def test_publish_failure_releases_lock_and_next_tick_publishes() -> None:
    redis = FakeRedis()
    publisher = RecordingPublisher(failures=1)
    runner = SchedulerRunner(_registry(), redis, publisher)
    runner.prime(_at("2026-11-15T23:00:00"))
    assert await runner.tick(_at("2026-11-16T00:00:01")) == {"t.daily": "error"}
    assert redis.store == {}
    assert await runner.tick(_at("2026-11-16T00:00:31")) == {"t.daily": "published"}
    assert len(publisher.messages) == 1
    assert runner.stats.runs == {("t.daily", "error"): 1, ("t.daily", "published"): 1}


@pytest.mark.parametrize(
    "error",
    [RedisConnectionError("down"), ResponseError("OOM command not allowed")],
)
async def test_redis_failure_fails_closed(error: Exception) -> None:
    redis = FakeRedis()
    redis.error = error
    publisher = RecordingPublisher()
    runner = SchedulerRunner(_registry(), redis, publisher)
    runner.prime(_at("2026-11-15T23:00:00"))
    assert await runner.tick(_at("2026-11-16T00:00:01")) == {"t.daily": "error"}
    assert publisher.messages == []
    redis.error = None
    assert await runner.tick(_at("2026-11-16T00:00:31")) == {"t.daily": "published"}


async def test_catch_up_publishes_missed_slot_once() -> None:
    redis = FakeRedis()
    publisher = RecordingPublisher()
    first = SchedulerRunner(_registry(at="03:00", catch_up=True), redis, publisher)
    first.prime(_at("2026-11-15T10:00:00"))
    assert await first.tick(_at("2026-11-15T10:00:00")) == {"t.daily": "published"}
    assert publisher.messages[0][1]["scheduled_at"] == "2026-11-15T03:00:00Z"
    second = SchedulerRunner(_registry(at="03:00", catch_up=True), redis, publisher)
    second.prime(_at("2026-11-15T10:05:00"))
    assert await second.tick(_at("2026-11-15T10:05:00")) == {"t.daily": "skipped"}
    no_catch_up = SchedulerRunner(_registry(at="03:00"), FakeRedis(), publisher)
    no_catch_up.prime(_at("2026-11-15T10:00:00"))
    assert await no_catch_up.tick(_at("2026-11-15T10:00:00")) == {}
    assert len(publisher.messages) == 1


def test_collector_exports_zero_series_for_registered_tasks() -> None:
    runner = SchedulerRunner(_registry(), FakeRedis(), RecordingPublisher())
    registry = CollectorRegistry()
    registry.register(SchedulerCollector(runner.stats, ["t.daily"]))
    text = generate_latest(registry).decode()
    for result in ("published", "skipped", "error"):
        assert f'scheduler_runs_total{{result="{result}",task="t.daily"}} 0.0' in text


def test_duplicate_task_is_rejected() -> None:
    registry = _registry()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(ScheduledTask("t.daily", Schedule.every(60), "tasks.t.daily"))


def test_scheduler_settings() -> None:
    settings = SchedulerSettings.model_validate({})
    assert settings.scheduler_enabled is True
    assert settings.scheduler_tick_seconds == 30
    assert settings.proxy_rehabilitate_at == "03:00"
    with pytest.raises(ValidationError):
        SchedulerSettings.model_validate({"proxy_rehabilitate_at": "25:00"})
