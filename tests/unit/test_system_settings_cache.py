"""``SystemSettingsCache`` (Redis snapshot + generation) and ``MaintenanceGate`` on top of it."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.modules.system import cache as cache_module
from app.modules.system.cache import (
    DEFAULT_SETTINGS,
    GENERATION_KEY,
    PG_FALLBACK_SECONDS,
    SNAPSHOT_KEY,
    SNAPSHOT_TTL_SECONDS,
    SettingsSnapshot,
    SystemSettingsCache,
)
from app.modules.system.maintenance import MaintenanceGate
from app.modules.system.models import DEFAULT_IAM_CACHE_TTL_SECONDS
from app.shared_kernel.domain.exceptions import EntityNotFoundError

pytestmark = pytest.mark.req("R-II.5")


def row(**overrides: Any) -> Any:
    base = {
        "is_maintenance_mode": False,
        "iam_cache_ttl_seconds": 600,
        "user_activity_retention_months": 3,
        "audit_logs_retention_months": 24,
        "file_logs_retention_days": 90,
        "extra_config": {},
    }
    return SimpleNamespace(**{**base, **overrides})


class FakeDb:
    """Session factory + ``SystemSettingsRepository`` stand-in."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.result: Any = row()
        self.reads = 0
        db = self

        class Repo:
            def __init__(self, session: Any) -> None:
                pass

            async def get(self) -> Any:
                db.reads += 1
                if isinstance(db.result, BaseException):
                    raise db.result
                return db.result

        monkeypatch.setattr(cache_module, "SystemSettingsRepository", Repo)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[Any]:
        yield MagicMock()


def make(redis: Any, db: FakeDb, now: list[float] | None = None) -> SystemSettingsCache:
    clock = (lambda: now[0]) if now is not None else (lambda: 0.0)
    return SystemSettingsCache(redis, cast(Any, db), clock=clock)


def redis_mock(mget: Any = None) -> MagicMock:
    redis = MagicMock()
    redis.mget = AsyncMock(return_value=mget if mget is not None else [None, None])
    redis.eval = AsyncMock(return_value=1)
    redis.incr = AsyncMock(return_value=1)
    redis.delete = AsyncMock(return_value=1)
    return redis


def test_snapshot_json_roundtrip_and_garbage() -> None:
    snap = SettingsSnapshot.from_model(cast(Any, row(is_maintenance_mode=True)))
    assert SettingsSnapshot.from_json(snap.to_json(7)) == (snap, 7)
    for raw in (b"garbage", b"{}", b'{"gen": 1, "is_maintenance_mode": "yes"}', b"[]"):
        assert SettingsSnapshot.from_json(raw) is None


async def test_hit_does_not_read_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDb(monkeypatch)
    cached = SettingsSnapshot(is_maintenance_mode=True)
    redis = redis_mock([cached.to_json(4), b"4"])
    result = await make(redis, db).get()
    assert result == cached
    assert db.reads == 0
    redis.mget.assert_awaited_once_with(SNAPSHOT_KEY, GENERATION_KEY)
    redis.eval.assert_not_awaited()


async def test_stale_generation_reads_postgres_and_guarded_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeDb(monkeypatch)
    db.result = row(iam_cache_ttl_seconds=1200)
    redis = redis_mock([SettingsSnapshot().to_json(3), b"4"])
    result = await make(redis, db).get()
    assert result.iam_cache_ttl_seconds == 1200
    assert db.reads == 1
    args = redis.eval.await_args.args
    assert args[1:4] == (2, SNAPSHOT_KEY, GENERATION_KEY)
    assert SettingsSnapshot.from_json(args[4]) == (result, 4)
    assert args[5:] == (SNAPSHOT_TTL_SECONDS, "4")


async def test_invalidate_between_read_and_write_skips_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeDb(monkeypatch)
    redis = redis_mock([None, b"5"])
    redis.eval.return_value = 0  # settings:gen bumped meanwhile -> Lua refuses the write
    result = await make(redis, db).get()
    assert result.iam_cache_ttl_seconds == 600  # the caller still gets the row it read
    assert redis.eval.await_args.args[-1] == "5"


async def test_missing_row_caches_defaults_in_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDb(monkeypatch)
    db.result = EntityNotFoundError("missing")
    redis = redis_mock([None, None])
    cache = make(redis, db)
    assert await cache.get() == DEFAULT_SETTINGS
    args = redis.eval.await_args.args
    assert SettingsSnapshot.from_json(args[4]) == (DEFAULT_SETTINGS, 0)
    assert args[5:] == (SNAPSHOT_TTL_SECONDS, "0")
    redis.mget.return_value = [args[4], None]  # what Redis now holds
    assert await cache.get() == DEFAULT_SETTINGS
    assert db.reads == 1  # second call served by Redis


def test_snapshot_ttl_is_60_seconds() -> None:
    assert SNAPSHOT_TTL_SECONDS == 60
    assert PG_FALLBACK_SECONDS == 5.0


async def test_redis_down_reuses_pg_snapshot_for_5_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    db = FakeDb(monkeypatch)
    redis = redis_mock()
    redis.mget.side_effect = RedisConnectionError("down")
    cache = make(redis, db, now)
    await cache.get()
    now[0] += 4.9
    await cache.get()
    assert db.reads == 1  # Redis error, snapshot younger than 5 s
    now[0] += 0.1  # 5 s since the PostgreSQL read
    db.result = row(is_maintenance_mode=True)
    assert (await cache.get()).is_maintenance_mode is True
    assert db.reads == 2


async def test_redis_up_never_uses_in_process_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDb(monkeypatch)
    redis = redis_mock([None, b"1"])
    redis.eval.return_value = 0  # nothing ever lands in Redis
    cache = make(redis, db)  # frozen clock: a fallback window would cover every call
    for _ in range(3):
        await cache.get()
    assert redis.mget.await_count == 3
    assert db.reads == 3  # Redis reachable -> no reuse of the PostgreSQL snapshot


async def test_corrupted_generation_is_a_miss_without_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeDb(monkeypatch)
    redis = redis_mock([SettingsSnapshot().to_json(0), b"not-an-int"])
    cache = make(redis, db)
    assert (await cache.get()).iam_cache_ttl_seconds == 600
    assert (await cache.get()).iam_cache_ttl_seconds == 600
    assert db.reads == 2  # every call reads PostgreSQL until the key is repaired
    redis.eval.assert_not_awaited()


async def test_connection_error_of_session_factory_is_swallowed() -> None:
    @asynccontextmanager
    async def broken() -> AsyncIterator[Any]:
        raise OSError("connection refused")
        yield  # pragma: no cover

    redis = redis_mock([None, b"0"])
    cache = SystemSettingsCache(redis, cast(Any, broken), clock=lambda: 0.0)
    assert await cache.get() == DEFAULT_SETTINGS
    redis.eval.assert_not_awaited()


async def test_breaker_open_reads_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    db = FakeDb(monkeypatch)
    db.result = row(is_maintenance_mode=True)
    redis = redis_mock()
    redis.mget.side_effect = RedisConnectionError("down")
    # No reuse window here (covered by the 5 s test): every call must read PostgreSQL;
    # the clock stays frozen so the breaker cooldown never expires.
    cache = SystemSettingsCache(redis, cast(Any, db), fallback_seconds=0.0, clock=lambda: now[0])
    for _ in range(3):
        assert (await cache.get()).is_maintenance_mode is True
    assert (await cache.get()).is_maintenance_mode is True
    assert redis.mget.await_count == 3  # breaker open: no Redis traffic
    assert db.reads == 4
    redis.eval.assert_not_awaited()  # unknown generation -> no write


async def test_postgres_error_returns_last_known(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDb(monkeypatch)
    db.result = row(is_maintenance_mode=True, iam_cache_ttl_seconds=900)
    redis = redis_mock([None, b"1"])
    cache = make(redis, db)
    assert (await cache.get()).is_maintenance_mode is True
    db.result = OperationalError("SELECT", {}, Exception("down"))
    redis.mget.return_value = [None, b"2"]
    last = await cache.get()
    assert (last.is_maintenance_mode, last.iam_cache_ttl_seconds) == (True, 900)


async def test_cold_start_defaults_when_everything_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDb(monkeypatch)
    db.result = OperationalError("SELECT", {}, Exception("down"))
    redis = redis_mock()
    redis.mget.side_effect = RedisConnectionError("down")
    result = await make(redis, db).get()
    assert result.is_maintenance_mode is False
    assert result.iam_cache_ttl_seconds == DEFAULT_IAM_CACHE_TTL_SECONDS


async def test_invalidate_bumps_generation_and_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDb(monkeypatch)
    redis = redis_mock()
    result = await make(redis, db).invalidate()
    assert (result.ok, result.removed) == (True, 1)
    redis.incr.assert_awaited_once_with(GENERATION_KEY)
    redis.delete.assert_awaited_once_with(SNAPSHOT_KEY)
    redis.incr.side_effect = RedisConnectionError("down")
    assert (await make(redis, db).invalidate()).ok is False


async def test_gate_reads_cache_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """No process-local TTL: a flag change in Redis is visible on the very next call."""
    db = FakeDb(monkeypatch)
    redis = redis_mock([SettingsSnapshot().to_json(1), b"1"])
    gate = MaintenanceGate(make(redis, db))
    assert await gate.is_on() is False
    redis.mget.return_value = [SettingsSnapshot(is_maintenance_mode=True).to_json(2), b"2"]
    assert await gate.is_on() is True
    assert redis.mget.await_count == 2
    assert db.reads == 0
