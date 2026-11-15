"""Master Bot component on ``run_service``: getMe at start, polling supervised (task 04)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import User
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.process import ServiceContext
from app.entrypoints.master_bot import app as master_app
from app.entrypoints.master_bot.app import MASTER_BOT_ID_KEY, master_bot_component
from app.modules.logging.ports import ActivityLogger

pytestmark = pytest.mark.req("R-II.3")


class _Container:
    def __init__(self) -> None:
        self.objects: dict[Any, Any] = {
            Redis: MagicMock(),
            async_sessionmaker[AsyncSession]: MagicMock(),
            ActivityLogger: MagicMock(),
        }

    async def get(self, key: Any) -> Any:
        return self.objects[key]


def _context(monkeypatch: pytest.MonkeyPatch) -> ServiceContext:
    monkeypatch.setenv("MASTER_BOT_TOKEN", "123456:ABCdef")
    monkeypatch.setenv("MASTER_BOT_USE_PROXY", "false")
    return ServiceContext(
        name="master_bot",
        settings=Settings(_env_file=None),
        container=_Container(),  # type: ignore[arg-type]
        metrics=MagicMock(),
    )


async def test_component_identifies_bot_and_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    me = User(id=777000, is_bot=True, first_name="master", username="master_bot")
    monkeypatch.setattr(Bot, "get_me", AsyncMock(return_value=me))
    monkeypatch.setattr(master_app, "setup_dishka", MagicMock())
    started = asyncio.Event()

    async def fake_polling(self: Dispatcher, *bots: Bot, **kwargs: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Dispatcher, "start_polling", fake_polling)
    monkeypatch.setattr(master_app, "POLLING_STOP_TIMEOUT", 0.05)
    ctx = _context(monkeypatch)
    async with master_bot_component(ctx) as runtime:
        await asyncio.wait_for(started.wait(), 1)
        assert runtime.telegram_bot_id == 777000
        assert runtime.dispatcher[MASTER_BOT_ID_KEY] == 777000
        polling = runtime.polling
        ctx.supervisor.begin_shutdown()  # as serve_components does before exit
    assert polling.done()
    assert ctx.supervisor.failed == []


async def test_component_fails_when_get_me_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Bot, "get_me", AsyncMock(side_effect=OSError("proxy down")))
    with pytest.raises(RuntimeError, match="getMe failed: OSError"):
        async with master_bot_component(_context(monkeypatch)):
            pass


async def test_component_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _context(monkeypatch)
    monkeypatch.setenv("MASTER_BOT_TOKEN", "")
    ctx = ServiceContext(
        name="master_bot",
        settings=Settings(_env_file=None),
        container=ctx.container,
        metrics=ctx.metrics,
    )
    with pytest.raises(RuntimeError, match="MASTER_BOT_TOKEN"):
        async with master_bot_component(ctx):
            pass
