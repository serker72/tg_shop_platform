"""Unit tests of :mod:`app.shared_kernel.infrastructure.generational_cache` and
:mod:`app.shared_kernel.infrastructure.redis_scripts` (mocked Redis)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.shared_kernel.infrastructure.generational_cache import (
    REDIS_ERRORS,
    GenerationalCache,
    InvalidationResult,
    RedisCircuitBreaker,
)
from app.shared_kernel.infrastructure.redis_scripts import (
    COMPARE_AND_DELETE_LUA,
    compare_and_delete,
)


def make(redis: MagicMock, now: list[float] | None = None) -> GenerationalCache:
    clock = (lambda: now[0]) if now is not None else (lambda: 0.0)
    return GenerationalCache(redis, breaker=RedisCircuitBreaker("test_cache", clock=clock))


def test_redis_errors_tuple() -> None:
    from redis.exceptions import RedisError

    assert REDIS_ERRORS == (RedisError, OSError, TimeoutError)


def test_iam_reexports_invalidation_result() -> None:
    from app.modules.iam import cache as iam_cache

    assert iam_cache.InvalidationResult is InvalidationResult


async def test_mget_and_read_generations() -> None:
    redis = MagicMock()
    redis.mget = AsyncMock(side_effect=[[b"v", None], [b"4", None]])
    cache = make(redis)
    assert await cache.mget("a", "b") == [b"v", None]
    redis.mget.assert_awaited_with("a", "b")
    assert await cache.read_generations("g1", "g2") == (4, 0)


async def test_set_if_unchanged_hit_and_stale() -> None:
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=1)
    cache = make(redis)
    assert await cache.set_if_unchanged("k", b"val", 0, {"g:user": 2, "g:all": 5}) is True
    args = redis.eval.await_args.args
    assert args[1:] == (3, "k", "g:user", "g:all", b"val", 1, "2", "5")  # ttl clamped to 1
    redis.eval.return_value = 0  # a guard changed meanwhile -> not written
    assert await cache.set_if_unchanged("k", b"val", 60, {"g:user": 2}) is False
    assert redis.eval.await_args.args[1:] == (2, "k", "g:user", b"val", 60, "2")


async def test_bump_incr_then_delete() -> None:
    redis = MagicMock()
    order: list[str] = []

    def incr(*_: str) -> int:
        order.append("incr")
        return 3

    def delete(*_: str) -> int:
        order.append("delete")
        return 2

    redis.incr = AsyncMock(side_effect=incr)
    redis.delete = AsyncMock(side_effect=delete)
    cache = make(redis)
    assert await cache.bump("gen", delete=["a", "b"]) == InvalidationResult(removed=2, ok=True)
    assert order == ["incr", "delete"]
    redis.delete.assert_awaited_once_with("a", "b")
    assert await cache.bump("gen") == InvalidationResult(removed=0, ok=True)
    assert redis.delete.await_count == 1  # nothing to delete -> no DEL


@pytest.mark.parametrize("error", [RedisConnectionError("down"), OSError("io"), TimeoutError()])
async def test_errors_are_swallowed(error: Exception) -> None:
    redis = MagicMock()
    for name in ("mget", "eval", "delete", "incr"):
        setattr(redis, name, AsyncMock(side_effect=error))
    cache = make(redis)
    assert await cache.mget("a") is None
    assert await cache.read_generations("g") is None
    assert await cache.set_if_unchanged("k", b"v", 10, {"g": 0}) is False
    # the third error opened the breaker: no further Redis calls
    assert (await cache.bump("g", delete=["k"])).ok is False
    assert await cache.delete("k") is None
    assert redis.incr.await_count == 0 and redis.delete.await_count == 0


async def test_breaker_opens_reopens_and_resets() -> None:
    now = [0.0]
    redis = MagicMock()
    redis.mget = AsyncMock(side_effect=RedisConnectionError("down"))
    cache = make(redis, now)
    for _ in range(3):
        assert await cache.mget("a") is None
    assert cache.available is False
    assert await cache.mget("a") is None
    assert redis.mget.await_count == 3  # no traffic while open
    now[0] += 6.0
    assert cache.available is True
    assert await cache.mget("a") is None  # failed probe -> open again
    assert cache.available is False
    now[0] += 6.0
    redis.mget.side_effect = None
    redis.mget.return_value = [None]
    assert await cache.mget("a") == [None]
    redis.mget.side_effect = RedisConnectionError("down")
    for _ in range(2):
        await cache.mget("a")
    assert cache.available is True  # failures counted from zero after the success


def test_breaker_log_events_use_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.shared_kernel.infrastructure import generational_cache as module

    events: list[str] = []
    fake = MagicMock()
    fake.warning.side_effect = lambda event, **_: events.append(event)
    fake.debug.side_effect = lambda event, **_: events.append(event)
    monkeypatch.setattr(module, "_log", fake)
    breaker = RedisCircuitBreaker("permission_cache", threshold=2, clock=lambda: 0.0)
    breaker.on_error("get_failed", OSError(), telegram_user_id=1)
    breaker.on_error("get_failed", OSError(), telegram_user_id=1)
    breaker.on_success()
    assert events == [
        "permission_cache_get_failed",
        "permission_cache_get_failed",
        "permission_cache_circuit_open",
        "permission_cache_recovered",
    ]


async def test_compare_and_delete() -> None:
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=1)
    assert await compare_and_delete(redis, "k", "tok") is True
    redis.eval.assert_awaited_once_with(COMPARE_AND_DELETE_LUA, 1, "k", "tok")
    redis.eval.return_value = 0
    assert await compare_and_delete(redis, "k", "tok") is False
