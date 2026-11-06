from __future__ import annotations

from typing import Any

import pytest
from dishka import make_async_container
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, QueuePool

from app.core.config import Settings
from app.shared_kernel.infrastructure import database, providers
from app.shared_kernel.infrastructure.database import build_engine, build_session_factory
from app.shared_kernel.infrastructure.providers import DatabaseProvider


def test_session_factory_does_not_expire_on_commit(settings: Settings) -> None:
    factory = build_session_factory(build_engine(settings, poolclass=NullPool))

    assert factory.kw["expire_on_commit"] is False
    assert factory.kw["autoflush"] is True


def test_default_engine_uses_pool_settings(settings: Settings) -> None:
    engine = build_engine(settings)

    assert isinstance(engine.pool, QueuePool)
    assert engine.pool.size() == settings.sqlalchemy.pool_size
    assert engine.url.drivername == "postgresql+psycopg"


def test_null_pool_engine_omits_pool_sizing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    real_create = create_async_engine

    def spy(url: str, **kwargs: Any) -> AsyncEngine:
        captured.update(kwargs)
        return real_create(url, **kwargs)

    monkeypatch.setattr(database, "create_async_engine", spy)

    engine = build_engine(settings, poolclass=NullPool)

    assert isinstance(engine.pool, NullPool)
    assert "pool_size" not in captured
    assert "max_overflow" not in captured
    assert captured["pool_pre_ping"] is True


async def test_database_provider_lifecycle(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_engine = build_engine(settings, poolclass=NullPool)
    built: list[Settings] = []
    disposed: list[AsyncEngine] = []
    closed: list[AsyncSession] = []
    original_close = AsyncSession.close

    def fake_build_engine(settings: Settings) -> AsyncEngine:
        built.append(settings)
        return real_engine

    async def fake_dispose(self: AsyncEngine, close: bool = True) -> None:
        disposed.append(self)

    async def spy_close(self: AsyncSession) -> None:
        closed.append(self)
        await original_close(self)

    monkeypatch.setattr(providers, "build_engine", fake_build_engine)
    monkeypatch.setattr(AsyncEngine, "dispose", fake_dispose)
    monkeypatch.setattr(AsyncSession, "close", spy_close)

    container = make_async_container(DatabaseProvider(), context={Settings: settings})
    first = await container.get(AsyncEngine)
    second = await container.get(AsyncEngine)
    factory = await container.get(async_sessionmaker[AsyncSession])

    assert first is second is real_engine
    assert built == [settings]
    assert factory.kw["expire_on_commit"] is False

    async with container() as request:
        session = await request.get(AsyncSession)
        assert session.bind is real_engine
        assert closed == []
    assert closed == [session]
    assert disposed == []

    await container.close()
    assert disposed == [real_engine]
