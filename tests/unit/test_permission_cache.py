"""Unit tests of :class:`PermissionCache` (versioned keys, breaker, mocked Redis)."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.modules.iam.cache import (
    GLOBAL_GENERATION_KEY,
    PERMISSION_KEY_PATTERN,
    PermissionCache,
)
from app.modules.iam.snapshot import PermissionSnapshot

pytestmark = pytest.mark.req("R3")

ERRORS = [RedisConnectionError("down"), RedisTimeoutError("slow"), OSError("io"), TimeoutError()]


def snapshot(global_gen: int = 0, user_gen: int = 0) -> PermissionSnapshot:
    return PermissionSnapshot(user_id=uuid4()).with_generations(
        user_generation=user_gen, global_generation=global_gen
    )


def test_keys() -> None:
    assert PermissionCache.key(42) == "user:42:permissions"
    assert PermissionCache.user_generation_key(42) == "user:42:perm_gen"
    assert GLOBAL_GENERATION_KEY == "perms:gen"
    assert PERMISSION_KEY_PATTERN == "user:*:permissions"


async def test_hit_compares_global_generation() -> None:
    redis = MagicMock()
    redis.mget = AsyncMock(
        side_effect=[
            [snapshot(global_gen=3, user_gen=1).to_json(), b"3"],  # fresh
            [snapshot(global_gen=3).to_json(), b"4"],  # global bumped -> stale
            [snapshot().to_json(), None],  # key never bumped -> 0 == 0
            [None, b"1"],
            [b"garbage", b"0"],
            [None, b"0"],
            [None, b"0"],
        ]
    )
    cache = PermissionCache(redis)
    hit = await cache.get_with_generations(1)
    assert hit is not None and hit[0].global_generation == 3
    assert await cache.get_with_generations(1) is None
    loaded = await cache.get_with_generations(1)
    assert loaded is not None and loaded[0].global_generation == 0
    assert await cache.get(1) is None
    assert await cache.get(1) is None
    assert await cache.get(1) is None
    assert await cache.get(1) is None
    redis.mget.assert_awaited_with("user:1:permissions", GLOBAL_GENERATION_KEY)


async def test_set_is_guarded_by_generations() -> None:
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=1)
    cache = PermissionCache(redis)
    original = snapshot()
    assert await cache.set(original, 7, 60, generations=(2, 5)) is True
    args = redis.eval.await_args.args
    assert args[1] == 3
    assert args[2:5] == ("user:7:permissions", "user:7:perm_gen", GLOBAL_GENERATION_KEY)
    assert PermissionSnapshot.from_json(args[5]) == original.with_generations(
        user_generation=2, global_generation=5
    )
    assert args[6:] == (60, "2", "5")
    redis.eval.return_value = 0  # generations changed meanwhile -> not written
    assert await cache.set(original, 7, 60, generations=(2, 5)) is False


async def test_read_generations() -> None:
    redis = MagicMock()
    redis.mget = AsyncMock(side_effect=[[b"4", None], [None, b"9"]])
    cache = PermissionCache(redis)
    assert await cache.read_generations(1) == (4, 0)
    assert await cache.read_generations(1) == (0, 9)


@pytest.mark.parametrize("error", ERRORS)
async def test_errors_are_swallowed(error: Exception) -> None:
    redis = MagicMock()
    for name in ("mget", "eval", "delete", "incr"):
        setattr(redis, name, AsyncMock(side_effect=error))
    cache = PermissionCache(redis)
    assert await cache.get(1) is None
    assert await cache.read_generations(1) is None
    assert await cache.set(snapshot(), 1, 10, generations=(0, 0)) is False
    result = await cache.invalidate(1)
    assert (result.removed, result.ok) == (0, False)
    assert (await cache.invalidate_all()).ok is False


async def test_timeout_on_get_and_set() -> None:
    redis = MagicMock()
    redis.mget = AsyncMock(side_effect=TimeoutError())
    redis.eval = AsyncMock(side_effect=TimeoutError())
    cache = PermissionCache(redis)
    assert await cache.get(1) is None
    assert await cache.set(snapshot(), 1, 10, generations=(0, 0)) is False


async def test_invalidate_bumps_user_generation() -> None:
    redis = MagicMock()
    redis.delete = AsyncMock(return_value=1)
    redis.incr = AsyncMock(return_value=3)
    call_order: list[str] = []

    def trace(name: str, value: int) -> Callable[[str], int]:
        def record(_: str) -> int:
            call_order.append(name)
            return value

        return record

    redis.incr.side_effect = trace("incr", 3)
    redis.delete.side_effect = trace("delete", 1)
    result = await PermissionCache(redis).invalidate(5)
    assert (result.removed, result.ok) == (1, True)
    redis.delete.assert_awaited_once_with("user:5:permissions")
    redis.incr.assert_awaited_once_with("user:5:perm_gen")
    assert call_order == ["incr", "delete"]  # RC-3: generation first


async def test_invalidate_returns_incr_value_ignored_but_snapshot_removed() -> None:
    redis = MagicMock()
    redis.incr = AsyncMock(return_value=7)
    redis.delete = AsyncMock(return_value=1)
    assert (await PermissionCache(redis).invalidate(5)).removed == 1
    redis.incr.return_value = 8  # generation counter keeps growing
    assert (await PermissionCache(redis).invalidate(5)).ok is True


async def test_invalidate_all_bumps_global_generation() -> None:
    redis = MagicMock()
    redis.incr = AsyncMock(return_value=2)
    result = await PermissionCache(redis).invalidate_all()
    assert (result.removed, result.ok) == (0, True)
    redis.incr.assert_awaited_once_with(GLOBAL_GENERATION_KEY)


async def test_breaker_reopens_after_cooldown_failure() -> None:
    now = [0.0]
    redis = MagicMock()
    redis.mget = AsyncMock(side_effect=RedisConnectionError("down"))
    cache = PermissionCache(redis, clock=lambda: now[0])
    for _ in range(3):
        await cache.get(1)
    assert cache.available is False
    now[0] += 6.0
    assert cache.available is True
    assert await cache.get(1) is None  # the probe fails -> breaker must close again
    assert cache.available is False
    assert await cache.get(1) is None
    assert redis.mget.await_count == 4


async def test_success_resets_breaker() -> None:
    now = [0.0]
    redis = MagicMock()
    redis.mget = AsyncMock(side_effect=RedisConnectionError("down"))
    cache = PermissionCache(redis, clock=lambda: now[0])
    for _ in range(3):
        await cache.get(1)
    now[0] += 6.0
    redis.mget.side_effect = None
    redis.mget.return_value = [None, b"0"]
    assert await cache.get(1) is None
    assert cache.available is True
    redis.mget.side_effect = RedisConnectionError("down")
    for _ in range(2):
        await cache.get(1)
    assert cache.available is True  # failures counted from zero after the success


async def test_circuit_breaker_skips_redis_after_threshold() -> None:
    now = [0.0]
    redis = MagicMock()
    redis.mget = AsyncMock(side_effect=RedisConnectionError("down"))
    cache = PermissionCache(redis, clock=lambda: now[0])
    for _ in range(3):
        assert await cache.get(1) is None
    assert cache.available is False
    assert await cache.get(1) is None
    assert redis.mget.await_count == 3  # no Redis traffic while the breaker is open
    assert await cache.set(snapshot(), 1, 10, generations=(0, 0)) is False
    assert (await cache.invalidate(1)).ok is False
    now[0] += 6.0
    assert cache.available is True
    redis.mget.side_effect = None
    redis.mget.return_value = [None, b"0"]
    assert await cache.get(1) is None
    assert cache.available is True
