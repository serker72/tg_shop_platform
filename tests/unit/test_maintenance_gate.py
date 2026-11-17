"""``MaintenanceGate``: TTL cache, single read on expiry, DB errors, missing row (task 04)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.exc import OperationalError

from app.modules.system import maintenance
from app.modules.system.maintenance import MaintenanceGate
from app.shared_kernel.domain.exceptions import EntityNotFoundError

pytestmark = pytest.mark.req("R-II.3")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Db:
    """Fake repository source: value, error or missing row; counts reads."""

    def __init__(self) -> None:
        self.value: bool | None = True
        self.error: Exception | None = None
        self.reads = 0

    def repository(self, session: Any) -> Any:
        db = self

        class _Repo:
            async def get(self) -> Any:
                db.reads += 1
                await asyncio.sleep(0)
                if db.error is not None:
                    raise db.error
                if db.value is None:
                    raise EntityNotFoundError("missing", code="system_settings_missing")
                return SimpleNamespace(is_maintenance_mode=db.value)

        return _Repo()


@asynccontextmanager
async def _session() -> AsyncIterator[None]:
    yield None


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> _Db:
    fake = _Db()
    monkeypatch.setattr(maintenance, "SystemSettingsRepository", fake.repository)
    return fake


def _gate(clock: _Clock) -> MaintenanceGate:
    return MaintenanceGate(cast(Any, _session), ttl=5, clock=clock)


async def test_cached_within_ttl_and_reloaded_after(db: _Db) -> None:
    clock = _Clock()
    gate = _gate(clock)
    assert await gate.is_on() is True
    db.value = False
    clock.now += 4.9
    assert await gate.is_on() is True and db.reads == 1
    clock.now += 0.2
    assert await gate.is_on() is False and db.reads == 2


async def test_invalidate_during_read_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read started before the commit must not cache the old value after ``invalidate``."""
    started, release = asyncio.Event(), asyncio.Event()
    state: dict[str, Any] = {"value": False, "reads": 0}

    class _Repo:
        def __init__(self, session: Any) -> None:
            pass

        async def get(self) -> Any:
            state["reads"] += 1
            value = state["value"]
            if state["reads"] == 1:
                started.set()
                await release.wait()
            return SimpleNamespace(is_maintenance_mode=value)

    monkeypatch.setattr(maintenance, "SystemSettingsRepository", _Repo)
    gate = _gate(_Clock())
    task = asyncio.create_task(gate.is_on())
    await started.wait()
    state["value"] = True  # committed while the first read is in flight
    gate.invalidate()
    release.set()
    assert await task is False
    assert await gate.is_on() is True
    assert state["reads"] == 2


async def test_concurrent_callers_share_one_read(db: _Db) -> None:
    gate = _gate(_Clock())
    results = await asyncio.gather(*(gate.is_on() for _ in range(5)))
    assert results == [True] * 5 and db.reads == 1


async def test_missing_row_is_off(db: _Db) -> None:
    db.value = None
    assert await _gate(_Clock()).is_on() is False


async def test_db_error_keeps_last_value_for_ttl(db: _Db) -> None:
    clock = _Clock()
    gate = _gate(clock)
    assert await gate.is_on() is True
    db.error = OperationalError("SELECT", {}, Exception("down"))
    clock.now += 6
    assert await gate.is_on() is True and db.reads == 2
    clock.now += 1
    assert await gate.is_on() is True and db.reads == 2  # cache extended by TTL


async def test_db_error_before_first_read_fails_open(db: _Db) -> None:
    db.error = OperationalError("SELECT", {}, Exception("down"))
    assert await _gate(_Clock()).is_on() is False
