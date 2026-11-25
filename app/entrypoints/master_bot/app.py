"""Master Bot process component: Bot (+proxy) -> getMe (retry) -> Dispatcher -> polling.

Plugged into :func:`app.core.process.run_service` as a ``ServiceComponent``. Entering the
component only starts a supervised task, so SIGTERM is handled while Telegram is still
unreachable: the task retries ``getMe`` with an exponential delay (1 s -> x2 -> 60 s, no
attempt limit) and keeps the ``telegram`` readiness gate not-ready until it succeeds
(``/health/live`` stays ok - compose does not restart the container). A rejected token
(401) is not retried: the task fails and the process exits with code 1.
Shutdown: stop polling (``POLLING_STOP_TIMEOUT``, then cancel) -> close the HTTP session;
the ``LogBuffer`` is drained when the Dishka container closes. ``RedisStorage`` is not
closed: it wraps the shared Redis client owned by the container.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final

import structlog
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import User
from aiogram.utils.i18n import ConstI18nMiddleware
from aiogram_dialog import setup_dialogs
from dishka import AsyncContainer
from dishka.integrations.aiogram import setup_dishka
from redis.asyncio import Redis

from app.core.i18n import DEFAULT_LOCALE, build_i18n
from app.core.observability.health import HeartbeatRegistry, ReadinessGate
from app.core.observability.tracing import AiogramTracingMiddleware
from app.entrypoints.master_bot.menu import build_menu_router
from app.entrypoints.master_bot.middlewares import (
    AuthMiddleware,
    ClickLogMiddleware,
    MaintenanceFlag,
    MaintenanceMiddleware,
    SnapshotLoader,
    UsernameSync,
)
from app.modules.bot_manager.sessions import build_bot_session, mask_proxy_url
from app.modules.iam.admin_service import UserAdminService
from app.modules.iam.service import PermissionService
from app.modules.iam.snapshot import PermissionSnapshot
from app.modules.logging.ports import ActivityLogger
from app.modules.system.maintenance import MaintenanceGate
from app.shared_kernel.presentation.aiogram_dialog_ids import install_unique_intent_ids

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.core.process import ServiceContext

__all__ = [
    "MASTER_BOT_ID_KEY",
    "TELEGRAM_READINESS",
    "MasterBotRunner",
    "build_dispatcher",
    "master_bot_component",
    "permission_loader",
    "resolve_master_bot_proxy",
    "username_sync",
]

#: Dispatcher workflow-data key of the Master Bot Telegram ID (``getMe().id``).
MASTER_BOT_ID_KEY: Final = "master_telegram_bot_id"
#: Readiness gate (critical ``/health/ready`` check) of the Telegram connection.
TELEGRAM_READINESS: Final = "telegram"
FSM_KEY_PREFIX: Final = "fsm"
#: Seconds to wait for polling to finish after ``stop_polling`` before cancelling it.
POLLING_STOP_TIMEOUT: float = 10.0
GET_ME_BACKOFF_INITIAL: float = 1.0
GET_ME_BACKOFF_MAX: float = 60.0

_log = structlog.get_logger(__name__)

DispatcherFactory = Callable[[int], Awaitable[Dispatcher]]


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


def username_sync(container: AsyncContainer) -> UsernameSync:
    """``UserAdminService.update_username`` in its own REQUEST scope (the service commits)."""

    async def sync(telegram_user_id: int, username: str | None) -> None:
        async with container() as request:
            service = await request.get(UserAdminService)
            await service.update_username(telegram_user_id, username)

    return sync


def build_dispatcher(
    *,
    redis: Redis,
    container: AsyncContainer,
    loader: SnapshotLoader,
    gate: MaintenanceFlag,
    activity: ActivityLogger,
    telegram_bot_id: int,
    sync: UsernameSync | None = None,
) -> Dispatcher:
    storage = RedisStorage(
        redis, key_builder=DefaultKeyBuilder(prefix=FSM_KEY_PREFIX, with_destiny=True)
    )
    dispatcher = Dispatcher(storage=storage)
    dispatcher[MASTER_BOT_ID_KEY] = telegram_bot_id
    dispatcher.update.outer_middleware(AiogramTracingMiddleware())
    i18n = build_i18n()
    if i18n is not None:
        dispatcher.update.outer_middleware(ConstI18nMiddleware(DEFAULT_LOCALE, i18n))
    dispatcher.update.outer_middleware(AuthMiddleware(loader, sync))
    dispatcher.update.outer_middleware(ClickLogMiddleware(activity, telegram_bot_id))
    dialog_isolation = SimpleEventIsolation()
    dispatcher.update.outer_middleware(MaintenanceMiddleware(gate, dialog_isolation))
    dispatcher.include_router(build_menu_router())
    install_unique_intent_ids()
    setup_dialogs(dispatcher, events_isolation=dialog_isolation)
    setup_dishka(container=container, router=dispatcher, auto_inject=True)
    return dispatcher


class MasterBotRunner:
    """getMe with retries, then polling; ``telegram_bot_id``/``dispatcher`` set on success."""

    def __init__(
        self,
        bot: Bot,
        *,
        build: DispatcherFactory,
        readiness: ReadinessGate,
        proxy: str | None,
    ) -> None:
        self.bot = bot
        self.telegram_bot_id: int | None = None
        self.dispatcher: Dispatcher | None = None
        self._build = build
        self._readiness = readiness
        self._proxy = proxy  # masked

    async def identify(self) -> User:
        delay = GET_ME_BACKOFF_INITIAL
        attempt = 0
        while True:
            attempt += 1
            try:
                # Cached alias of getMe: ``start_polling`` reuses ``_me`` (no extra request).
                return await self.bot.me()
            except TelegramUnauthorizedError:
                _log.critical("master_bot_token_rejected", proxy=self._proxy)
                raise RuntimeError("Master Bot token rejected by Telegram (401)") from None
            except Exception as error:
                # Type only: messages may carry the request URL (token) or proxy details.
                _log.warning(
                    "master_bot_get_me_failed",
                    error=type(error).__name__,
                    attempt=attempt,
                    retry_in=delay,
                    proxy=self._proxy,
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, GET_ME_BACKOFF_MAX)

    async def run(self) -> None:
        """getMe (retries, readiness not-ready) -> Dispatcher -> ready -> polling.

        Readiness is checked only up to the first successful getMe: if Telegram becomes
        unreachable later, ``telegram`` stays ready and aiogram polling retries the
        ``getUpdates`` requests itself.
        """
        me = await self.identify()
        _log.info(
            "master_bot_identified", telegram_bot_id=me.id, username=me.username, proxy=self._proxy
        )
        self.telegram_bot_id = me.id
        self.dispatcher = await self._build(me.id)
        self._readiness.set_ready()
        await self.dispatcher.start_polling(self.bot, handle_signals=False, close_bot_session=False)


async def _stop(dispatcher: Dispatcher | None, task: asyncio.Task[None]) -> None:
    stopping = False
    if dispatcher is not None:
        try:
            await dispatcher.stop_polling()
            stopping = True
        except RuntimeError:  # polling not started yet (still waiting for getMe)
            pass
    if stopping:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=POLLING_STOP_TIMEOUT)
        except TimeoutError:
            _log.warning("master_bot_polling_stop_timeout", timeout=POLLING_STOP_TIMEOUT)
        except Exception:  # noqa: S110 - the supervisor already reported the failure
            pass
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


@asynccontextmanager
async def master_bot_component(ctx: ServiceContext) -> AsyncIterator[MasterBotRunner]:
    settings = ctx.settings
    token = settings.telegram.master_bot_token
    if token is None:
        raise RuntimeError("MASTER_BOT_TOKEN is not set")
    container = ctx.container
    registry = await container.get(HeartbeatRegistry)
    readiness = registry.readiness.get(TELEGRAM_READINESS) or ReadinessGate()
    readiness.set_not_ready()
    redis = await container.get(Redis)
    # Reads the Redis settings snapshot (SystemSettingsCache): changes of any process apply.
    gate = await container.get(MaintenanceGate)
    activity = await container.get(ActivityLogger)

    async def build(telegram_bot_id: int) -> Dispatcher:
        return build_dispatcher(
            redis=redis,
            container=container,
            loader=permission_loader(container),
            gate=gate,
            activity=activity,
            telegram_bot_id=telegram_bot_id,
            sync=username_sync(container),
        )

    proxy_url = resolve_master_bot_proxy(settings)
    bot = Bot(token.get_secret_value(), session=build_bot_session(proxy_url))
    runner = MasterBotRunner(
        bot,
        build=build,
        readiness=readiness,
        proxy=mask_proxy_url(proxy_url) if proxy_url else None,
    )
    task = asyncio.create_task(runner.run(), name="master_bot.polling")
    ctx.supervisor.watch(task)
    try:
        yield runner
    finally:
        readiness.set_not_ready()
        await _stop(runner.dispatcher, task)
        await bot.session.close()
