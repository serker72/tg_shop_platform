"""Master Bot component on ``run_service``: getMe retry, readiness, polling stop (task 04)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import User
from aiogram.utils.i18n import ConstI18nMiddleware
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.core.config import Settings
from app.core.observability.health import HealthChecker, HeartbeatRegistry
from app.core.observability.tracing import AiogramTracingMiddleware
from app.core.process import ServiceContext
from app.entrypoints.master_bot import app as master_app
from app.entrypoints.master_bot.app import (
    MASTER_BOT_ID_KEY,
    TELEGRAM_READINESS,
    build_dispatcher,
    master_bot_component,
)
from app.entrypoints.master_bot.middlewares import (
    AuthMiddleware,
    ClickLogMiddleware,
    MaintenanceMiddleware,
)
from app.modules.logging.ports import ActivityLogger
from app.modules.system.maintenance import MaintenanceGate

pytestmark = pytest.mark.req("R-II.3")

TOKEN = "123456:ABCdefSecretToken"
PROXY = "socks5://user:proxypass@10.0.0.1:1080"
ME = User(id=777000, is_bot=True, first_name="master", username="master_bot")


class _Container:
    def __init__(self) -> None:
        self.registry = HeartbeatRegistry()
        self.gate = self.registry.register_readiness(TELEGRAM_READINESS)
        self.objects: dict[Any, Any] = {
            Redis: MagicMock(),
            async_sessionmaker[AsyncSession]: MagicMock(),
            MaintenanceGate: MagicMock(),
            ActivityLogger: MagicMock(),
            HeartbeatRegistry: self.registry,
        }

    async def get(self, key: Any) -> Any:
        return self.objects[key]


def _context(monkeypatch: pytest.MonkeyPatch, *, proxy: bool = False) -> ServiceContext:
    monkeypatch.setenv("MASTER_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("MASTER_BOT_USE_PROXY", "true" if proxy else "false")
    monkeypatch.setenv("MASTER_BOT_PROXY_URL", PROXY if proxy else "")
    return ServiceContext(
        name="master_bot",
        settings=Settings(_env_file=None),
        container=_Container(),  # type: ignore[arg-type]
        metrics=MagicMock(),
    )


def _gate(ctx: ServiceContext) -> Any:
    return ctx.container.gate  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(master_app, "setup_dishka", MagicMock())
    monkeypatch.setattr(master_app, "GET_ME_BACKOFF_INITIAL", 0.001)
    monkeypatch.setattr(master_app, "GET_ME_BACKOFF_MAX", 0.004)


def _forever_polling(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    started = asyncio.Event()

    async def fake_polling(self: Dispatcher, *bots: Bot, **kwargs: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Dispatcher, "start_polling", fake_polling)
    return started


async def test_component_identifies_bot_and_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Bot, "get_me", AsyncMock(return_value=ME))
    started = _forever_polling(monkeypatch)
    ctx = _context(monkeypatch)
    async with master_bot_component(ctx) as runner:
        await asyncio.wait_for(started.wait(), 1)
        assert runner.telegram_bot_id == 777000
        assert runner.dispatcher is not None
        assert runner.dispatcher[MASTER_BOT_ID_KEY] == 777000
        assert _gate(ctx).ready
        ctx.supervisor.begin_shutdown()  # as serve_components does before exit
    assert not _gate(ctx).ready
    assert ctx.supervisor.failed == []


async def test_identify_caches_get_me(monkeypatch: pytest.MonkeyPatch) -> None:
    get_me = AsyncMock(return_value=ME)
    monkeypatch.setattr(Bot, "get_me", get_me)
    bot = Bot(TOKEN)
    try:
        runner = master_app.MasterBotRunner(
            bot, build=AsyncMock(), readiness=MagicMock(), proxy=None
        )
        assert await runner.identify() == ME
        assert await bot.me() == ME  # start_polling calls bot.me(): no second request
        assert get_me.await_count == 1
    finally:
        await bot.session.close()


async def test_get_me_retried_with_backoff_without_secrets_in_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = OSError(f"Cannot connect via {PROXY} to https://api.telegram.org/bot{TOKEN}/getMe")
    get_me = AsyncMock(side_effect=[error, error, ME])
    monkeypatch.setattr(Bot, "get_me", get_me)
    started = _forever_polling(monkeypatch)
    ctx = _context(monkeypatch, proxy=True)
    with capture_logs() as logs:
        async with master_bot_component(ctx):
            await asyncio.wait_for(started.wait(), 1)
            ctx.supervisor.begin_shutdown()
    failures = [entry for entry in logs if entry["event"] == "master_bot_get_me_failed"]
    assert [entry["attempt"] for entry in failures] == [1, 2]
    assert failures[1]["retry_in"] == 2 * failures[0]["retry_in"]
    assert failures[0]["proxy"] == "socks5://10.0.0.1:1080"
    assert TOKEN not in str(logs) and "proxypass" not in str(logs)
    assert get_me.await_count == 3


async def test_not_ready_while_telegram_unreachable_and_clean_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    third_attempt = asyncio.Event()
    attempts = 0

    async def failing_get_me(self: Bot, *args: Any, **kwargs: Any) -> User:
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            third_attempt.set()
        raise OSError("down")

    get_me = failing_get_me
    close = AsyncMock()
    monkeypatch.setattr(Bot, "get_me", get_me)
    monkeypatch.setattr(AiohttpSession, "close", close)
    ctx = _context(monkeypatch)
    async with master_bot_component(ctx) as runner:
        await asyncio.wait_for(third_attempt.wait(), 1)
        assert not _gate(ctx).ready and runner.dispatcher is None
        report = await HealthChecker(
            ctx.container.registry.checks(MagicMock(), ttl=1),  # type: ignore[attr-defined]
            timeout=1,
        ).run()
        assert not report.ready
        assert report.as_dict()["checks"][TELEGRAM_READINESS]["status"] == "fail"
        ctx.supervisor.begin_shutdown()
    close.assert_awaited()  # session closed although getMe never succeeded
    assert ctx.supervisor.failed == []


async def test_rejected_token_fails_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    unauthorized = TelegramUnauthorizedError(method=MagicMock(), message="Unauthorized")
    monkeypatch.setattr(Bot, "get_me", AsyncMock(side_effect=unauthorized))
    ctx = _context(monkeypatch)
    async with master_bot_component(ctx):
        await asyncio.wait_for(ctx.supervisor.stop_requested.wait(), 1)
        ctx.supervisor.begin_shutdown()
    assert ctx.supervisor.failed == ["master_bot.polling"]


async def test_polling_stop_timeout_cancels_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Bot, "get_me", AsyncMock(return_value=ME))
    started = _forever_polling(monkeypatch)
    stop = AsyncMock()
    monkeypatch.setattr(Dispatcher, "stop_polling", stop)
    monkeypatch.setattr(master_app, "POLLING_STOP_TIMEOUT", 0.05)
    ctx = _context(monkeypatch)
    with capture_logs() as logs:
        async with master_bot_component(ctx):
            await asyncio.wait_for(started.wait(), 1)
            ctx.supervisor.begin_shutdown()
    stop.assert_awaited_once()
    assert any(entry["event"] == "master_bot_polling_stop_timeout" for entry in logs)
    assert ctx.supervisor.failed == []


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


def test_outer_middleware_order() -> None:
    dispatcher = build_dispatcher(
        redis=MagicMock(),
        container=MagicMock(),
        loader=AsyncMock(),
        gate=MagicMock(),
        activity=MagicMock(),
        telegram_bot_id=1,
    )
    names: list[type] = [type(m) for m in dispatcher.update.outer_middleware]
    expected = [
        AiogramTracingMiddleware,
        ConstI18nMiddleware,
        AuthMiddleware,
        ClickLogMiddleware,
        MaintenanceMiddleware,
    ]
    positions = [names.index(cls) for cls in expected]
    assert positions == sorted(positions)


def test_readiness_gate_is_critical_check() -> None:
    registry = HeartbeatRegistry()
    gate = registry.register_readiness("telegram")
    assert registry.register_readiness("telegram") is gate
    (check,) = registry.checks(MagicMock(), ttl=1)
    assert (check.name, check.critical) == ("telegram", True)
