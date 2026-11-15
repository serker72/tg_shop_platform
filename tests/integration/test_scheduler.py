"""Scheduler slot lock against the test Redis (DB 15)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.scheduler import Schedule, ScheduledTask, SchedulerRunner, TaskRegistry

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.15")]


class _Publisher:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.messages: list[dict[str, Any]] = []

    async def publish(self, queue: str, message: dict[str, Any], *, message_id: str) -> None:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("broker down")
        self.messages.append(message)


@pytest.fixture
async def redis(redis_client: Any) -> AsyncIterator[Any]:
    async def drop() -> None:
        keys = [key async for key in redis_client.scan_iter(match="sched:lock:it.*", count=500)]
        if keys:
            await redis_client.delete(*keys)

    await drop()
    yield redis_client
    await drop()


def _registry() -> TaskRegistry:
    registry = TaskRegistry()
    registry.register(
        ScheduledTask("it.daily", Schedule.daily_at("03:00"), "tasks.it.daily", catch_up=True)
    )
    return registry


async def test_lock_key_with_ttl_and_single_leader(redis: Any) -> None:
    now = datetime(2026, 11, 15, 3, 0, 5, tzinfo=UTC)
    publisher = _Publisher()
    leader = SchedulerRunner(_registry(), redis, publisher)
    follower = SchedulerRunner(_registry(), redis, publisher)
    assert await leader.tick(now) == {"it.daily": "published"}
    assert await follower.tick(now) == {"it.daily": "skipped"}
    key = "sched:lock:it.daily:2026-11-15T03:00:00Z"
    ttl = await redis.ttl(key)
    assert 24 * 3600 < ttl <= 25 * 3600
    assert len(publisher.messages) == 1


async def test_publish_failure_drops_lock(redis: Any) -> None:
    now = datetime(2026, 11, 15, 3, 0, 5, tzinfo=UTC)
    publisher = _Publisher(failures=1)
    runner = SchedulerRunner(_registry(), redis, publisher)
    assert await runner.tick(now) == {"it.daily": "error"}
    assert await redis.exists("sched:lock:it.daily:2026-11-15T03:00:00Z") == 0
    assert await runner.tick(now) == {"it.daily": "published"}
