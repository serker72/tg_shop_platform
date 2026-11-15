"""Master Bot process component: Bot (+proxy) -> getMe -> Dispatcher -> polling.

Plugged into :func:`app.core.process.run_service` as a ``ServiceComponent``; the polling task
is supervised (its death stops the process). Shutdown: stop polling -> close the HTTP session;
the ``LogBuffer`` is drained when the Dishka container closes.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.i18n import ConstI18nMiddleware
from aiogram_dialog import setup_dialogs
from dishka import AsyncContainer
from dishka.integrations.aiogram import setup_dishka
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.i18n import DEFAULT_LOCALE, build_i18n
from app.core.observability.tracing import AiogramTracingMiddleware
from app.entrypoints.master_bot.menu import build_menu_router
from app.entrypoints.master_bot.middlewares import (
    AuthMiddleware,
    ClickLogMiddleware,
    MaintenanceFlag,
    MaintenanceMiddleware,
    SnapshotLoader,
)
from app.modules.bot_manager.sessions import build_bot_session, mask_proxy_url
from app.modules.iam.service import PermissionService
from app.modules.iam.snapshot import PermissionSnapshot
from app.modules.logging.ports import ActivityLogger
from app.modules.system.maintenance import MaintenanceGate

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.core.process import ServiceContext

__all__ = [
    "MASTER_BOT_ID_KEY",
    "MasterBotRuntime",
    "build_dispatcher",
    "master_bot_component",
    "resolve_master_bot_proxy",
]

#: Dispatcher workflow-data key of the Master Bot Telegram ID (``getMe().id``).
MASTER_BOT_ID_KEY: Final = "master_telegram_bot_id"
FSM_KEY_PREFIX: Final = "fsm"
#: Seconds to wait for polling to finish after ``stop_polling`` before cancelling it.
POLLING_STOP_TIMEOUT: float = 10.0

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MasterBotRuntime:
    bot: Bot
    dispatcher: Dispatcher
    telegram_bot_id: int
    polling: asyncio.Task[None]


def resolve_master_bot_proxy(settings: Settings) -> str | None:
    """``MASTER_BOT_PROXY_URL`` when ``MASTER_BOT_USE_PROXY=true``, else direct (``None``)."""
    proxy = settings.proxy
    if not proxy.master_bot_use_proxy or proxy.master_bot_proxy_url is None:
        return None
    return proxy.master_bot_proxy_url.get_secret_value()


def permission_loader(container: AsyncContainer) -> SnapshotLoader:
    """Snapshot through ``PermissionService`` in its own Dishka REQUEST scope."""

    async def load(telegram_user_id: int) -> PermissionSnapshot:
        async with container() as request:
            service = await request.get(PermissionService)
            return await service.get_snapshot(telegram_user_id)

    return load


def build_dispatcher(
    *,
    redis: Redis,
    container: AsyncContainer,
    loader: SnapshotLoader,
    gate: MaintenanceFlag,
    activity: ActivityLogger,
    telegram_bot_id: int,
) -> Dispatcher:
    storage = RedisStorage(
        redis, key_builder=DefaultKeyBuilder(prefix=FSM_KEY_PREFIX, with_destiny=True)
    )
    dispatcher = Dispatcher(storage=storage)
    dispatcher[MASTER_BOT_ID_KEY] = telegram_bot_id
    dispatcher.update.outer_middleware(AiogramTracingMiddleware())
    dispatcher.update.outer_middleware(ConstI18nMiddleware(DEFAULT_LOCALE, build_i18n()))
    dispatcher.update.outer_middleware(AuthMiddleware(loader))
    dispatcher.update.outer_middleware(MaintenanceMiddleware(gate))
    dispatcher.update.outer_middleware(ClickLogMiddleware(activity, telegram_bot_id))
    dispatcher.include_router(build_menu_router())
    setup_dialogs(dispatcher)
    setup_dishka(container=container, router=dispatcher, auto_inject=True)
    return dispatcher


@asynccontextmanager
async def master_bot_component(ctx: ServiceContext) -> AsyncIterator[MasterBotRuntime]:
    settings = ctx.settings
    token = settings.telegram.master_bot_token
    if token is None:
        raise RuntimeError("MASTER_BOT_TOKEN is not set")
    proxy_url = resolve_master_bot_proxy(settings)
    masked = mask_proxy_url(proxy_url) if proxy_url else None
    bot = Bot(token.get_secret_value(), session=build_bot_session(proxy_url))
    try:
        me = await bot.get_me()
    except Exception as error:
        await bot.session.close()
        # Type only: the message may carry the request URL (token) or proxy details.
        _log.critical("master_bot_get_me_failed", error=type(error).__name__, proxy=masked)
        raise RuntimeError(f"Master Bot getMe failed: {type(error).__name__}") from None
    _log.info("master_bot_identified", telegram_bot_id=me.id, username=me.username, proxy=masked)
    container = ctx.container
    dispatcher = build_dispatcher(
        redis=await container.get(Redis),
        container=container,
        loader=permission_loader(container),
        gate=MaintenanceGate(await container.get(async_sessionmaker[AsyncSession])),
        activity=await container.get(ActivityLogger),
        telegram_bot_id=me.id,
    )
    polling = asyncio.create_task(
        dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False),
        name="master_bot.polling",
    )
    ctx.supervisor.watch(polling)
    try:
        yield MasterBotRuntime(bot, dispatcher, me.id, polling)
    finally:
        with contextlib.suppress(RuntimeError):  # "Polling is not started"
            await dispatcher.stop_polling()
        try:
            await asyncio.wait_for(asyncio.shield(polling), timeout=POLLING_STOP_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError, Exception):
            polling.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await polling
        await bot.session.close()
