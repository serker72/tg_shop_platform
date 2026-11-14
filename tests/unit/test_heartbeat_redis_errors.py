"""``publish_heartbeat`` survives Redis OOM (maxmemory + noeviction) and connection errors."""

from __future__ import annotations

from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from app.core.observability.health import publish_heartbeat


class _Pipe:
    def __init__(self, exc: Exception | None) -> None:
        self.exc = exc
        self.commands: list[str] = []

    async def __aenter__(self) -> _Pipe:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def zadd(self, *args: Any) -> None:
        self.commands.append("zadd")

    def zremrangebyscore(self, *args: Any) -> None:
        self.commands.append("zremrangebyscore")

    def expire(self, *args: Any) -> None:
        self.commands.append("expire")

    async def execute(self) -> list[object]:
        if self.exc is not None:
            raise self.exc
        return []


class _Redis:
    def __init__(self, exc: Exception | None) -> None:
        self.pipe = _Pipe(exc)

    def pipeline(self, *, transaction: bool = True) -> _Pipe:
        return self.pipe


@pytest.mark.parametrize(
    "exc",
    [
        ResponseError("OOM command not allowed when used memory > 'maxmemory'."),
        RedisConnectionError("Connection refused"),
    ],
)
async def test_publish_heartbeat_does_not_raise_on_redis_errors(exc: Exception) -> None:
    redis = _Redis(exc)

    ok = await publish_heartbeat(redis, "bot_worker", "i-1", ttl=30, now=100.0)  # type: ignore[arg-type]

    assert ok is False
    assert redis.pipe.commands == ["zadd", "zremrangebyscore", "expire"]


async def test_publish_heartbeat_returns_true_on_success() -> None:
    ok = await publish_heartbeat(_Redis(None), "bot_worker", "i-1", ttl=30, now=100.0)  # type: ignore[arg-type]

    assert ok is True
