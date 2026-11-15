"""Outer update middlewares of the Master Bot (order: tracing -> i18n -> Auth -> Maintenance
-> ClickLog, see :func:`app.entrypoints.master_bot.app.build_dispatcher`).

Middlewares only read state and enqueue click-log events: no DB writes (services own them).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final, Protocol

import structlog
from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject, Update, User

from app.entrypoints.master_bot.texts import Texts
from app.modules.iam.permissions import SETTINGS_MANAGE
from app.modules.iam.snapshot import PermissionSnapshot
from app.modules.logging.events import ActivityEvent
from app.modules.logging.ports import ActivityLogger

__all__ = [
    "SNAPSHOT_KEY",
    "SOURCE",
    "AuthMiddleware",
    "ClickLogMiddleware",
    "MaintenanceMiddleware",
    "SnapshotLoader",
    "has_panel_access",
]

#: ``data`` key of the :class:`PermissionSnapshot` of the update author.
SNAPSHOT_KEY: Final = "permission_snapshot"
#: ``user_activity.source`` of the Master Bot events.
SOURCE: Final = "master_bot"

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]
SnapshotLoader = Callable[[int], Awaitable[PermissionSnapshot]]

_log = structlog.get_logger(__name__)


class MaintenanceFlag(Protocol):
    async def is_on(self) -> bool: ...


def has_panel_access(snapshot: PermissionSnapshot) -> bool:
    """Live user that is a superuser or holds at least one section of the main menu."""
    from app.entrypoints.master_bot.menu import MENU_PERMISSIONS

    if not snapshot.exists:
        return False
    if snapshot.is_superuser or snapshot.tenants:
        return True
    return any(snapshot.decide(slug).allowed for slug in MENU_PERMISSIONS)


class AuthMiddleware(BaseMiddleware):
    """Unknown / inactive / deleted users or users without rights are ignored silently (Q2)."""

    def __init__(self, loader: SnapshotLoader) -> None:
        self._loader = loader

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.is_bot:
            return None
        snapshot = await self._loader(user.id)
        if not has_panel_access(snapshot):
            _log.info("master_bot_access_ignored", telegram_user_id=user.id)
            return None
        data[SNAPSHOT_KEY] = snapshot
        return await handler(event, data)


class MaintenanceMiddleware(BaseMiddleware):
    """Maintenance on: stub for everyone except superuser and ``settings.manage`` (Q4)."""

    def __init__(self, gate: MaintenanceFlag) -> None:
        self._gate = gate

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        snapshot: PermissionSnapshot | None = data.get(SNAPSHOT_KEY)
        if snapshot is None or not await self._gate.is_on():
            return await handler(event, data)
        if snapshot.decide(SETTINGS_MANAGE).allowed:  # superuser included
            return await handler(event, data)
        await _send_stub(event, data.get("bot"), Texts.maintenance())
        return None


async def _send_stub(event: TelegramObject, bot: Bot | None, text: str) -> None:
    if bot is None or not isinstance(event, Update):
        return
    if event.callback_query is not None:
        await bot.answer_callback_query(event.callback_query.id, text=text, show_alert=True)
    elif event.message is not None:
        await bot.send_message(event.message.chat.id, text)


class ClickLogMiddleware(BaseMiddleware):
    """Enqueue an ``ActivityEvent`` (``telegram_bot_id`` = Master Bot ``getMe().id``)."""

    def __init__(self, activity: ActivityLogger, telegram_bot_id: int) -> None:
        self._activity = activity
        self._telegram_bot_id = telegram_bot_id

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")
        if isinstance(event, Update) and user is not None:
            action_type, callback_data, button = _describe(event)
            try:
                self._activity.log(
                    ActivityEvent(
                        telegram_bot_id=self._telegram_bot_id,
                        action_type=action_type,
                        tenant_id=None,
                        telegram_user_id=user.id,
                        username=user.username,
                        user_type="staff",
                        source=SOURCE,
                        callback_data=callback_data,
                        button=button,
                    )
                )
            except Exception as error:  # click-log must never break the update
                _log.warning("master_bot_click_log_failed", error=type(error).__name__)
        return await handler(event, data)


def _describe(update: Update) -> tuple[str, str | None, str | None]:
    if update.callback_query is not None:
        return "callback", update.callback_query.data, None
    if update.message is not None:
        text = update.message.text or ""
        return ("command" if text.startswith("/") else "message"), None, text[:64] or None
    return update.event_type, None, None
