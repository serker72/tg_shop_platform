"""Outer update middlewares of the Master Bot (order: tracing -> i18n -> Auth -> ClickLog
-> Maintenance, see :func:`app.entrypoints.master_bot.app.build_dispatcher`).

Middlewares never write the DB themselves: click-log goes to the ``ActivityLogger`` buffer,
the username pick-up (Q3) goes through ``UserAdminService`` in its own request scope.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final, Protocol

import structlog
from aiogram import BaseMiddleware, Bot
from aiogram.types import Chat, TelegramObject, Update, User

from app.entrypoints.master_bot.texts import Texts
from app.modules.iam.permissions import SETTINGS_MANAGE
from app.modules.iam.snapshot import PermissionSnapshot
from app.modules.logging.events import ActivityEvent
from app.modules.logging.models import utc_now
from app.modules.logging.ports import ActivityLogger

__all__ = [
    "MAINTENANCE_BLOCKED_KEY",
    "MAINTENANCE_RESULT",
    "SNAPSHOT_KEY",
    "SOURCE",
    "TEXT_MARKER",
    "AuthMiddleware",
    "ClickLogMiddleware",
    "MaintenanceMiddleware",
    "SnapshotLoader",
    "UsernameSync",
    "has_panel_access",
]

#: ``data`` key of the :class:`PermissionSnapshot` of the update author.
SNAPSHOT_KEY: Final = "permission_snapshot"
#: ``data`` flag set by :class:`MaintenanceMiddleware` when it answered with the stub.
MAINTENANCE_BLOCKED_KEY: Final = "maintenance_blocked"
#: ``user_activity.result`` of an update blocked by maintenance.
MAINTENANCE_RESULT: Final = "maintenance"
#: ``user_activity.source`` of the Master Bot events.
SOURCE: Final = "master_bot"
#: Logged instead of a free text message (tokens, PII never reach the click-log).
TEXT_MARKER: Final = "<text>"


Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]
SnapshotLoader = Callable[[int], Awaitable[PermissionSnapshot]]
#: ``(telegram_user_id, username)`` -> stores the username (own transaction).
UsernameSync = Callable[[int, str | None], Awaitable[None]]

_log = structlog.get_logger(__name__)
_UNKNOWN: Final = object()


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
    """Only private chats; unknown / inactive / deleted / no rights -> silence (Q2).

    After a successful check the Telegram username is synced (Q3) when it differs from the
    last value seen by this process (best effort: an error is logged, the update goes on).
    """

    def __init__(self, loader: SnapshotLoader, username_sync: UsernameSync | None = None) -> None:
        self._loader = loader
        self._username_sync = username_sync
        self._usernames: dict[int, str | None] = {}

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")
        chat: Chat | None = data.get("event_chat")
        if user is None or user.is_bot or chat is None or chat.type != "private":
            return None
        snapshot = await self._loader(user.id)
        if not has_panel_access(snapshot):
            _log.info("master_bot_access_ignored", telegram_user_id=user.id)
            return None
        data[SNAPSHOT_KEY] = snapshot
        await self._sync_username(user)
        return await handler(event, data)

    async def _sync_username(self, user: User) -> None:
        if self._username_sync is None or self._usernames.get(user.id, _UNKNOWN) == user.username:
            return
        try:
            await self._username_sync(user.id, user.username)
        except Exception as error:  # best effort: never blocks the panel
            _log.warning(
                "master_bot_username_sync_failed",
                telegram_user_id=user.id,
                error=type(error).__name__,
            )
            return
        self._usernames[user.id] = user.username


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
        data[MAINTENANCE_BLOCKED_KEY] = True
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
    """Enqueue an ``ActivityEvent`` (``telegram_bot_id`` = Master Bot ``getMe().id``).

    Runs after Auth (unauthorised updates are never logged) and wraps Maintenance: an update
    answered with the stub is logged with ``result="maintenance"``. Free text is replaced by
    :data:`TEXT_MARKER`; only the command word of ``/commands`` is kept.
    """

    def __init__(self, activity: ActivityLogger, telegram_bot_id: int) -> None:
        self._activity = activity
        self._telegram_bot_id = telegram_bot_id

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")
        if not isinstance(event, Update) or user is None:
            return await handler(event, data)
        created_at = utc_now()
        try:
            return await handler(event, data)
        finally:
            self._log(event, user, data, created_at)

    def _log(self, update: Update, user: User, data: dict[str, Any], created_at: Any) -> None:
        action_type, callback_data, button = describe_update(update)
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
                    result=MAINTENANCE_RESULT if data.get(MAINTENANCE_BLOCKED_KEY) else None,
                    created_at=created_at,
                )
            )
        except Exception as error:  # click-log must never break the update
            _log.warning("master_bot_click_log_failed", error=type(error).__name__)


def describe_update(update: Update) -> tuple[str, str | None, str | None]:
    """``(action_type, callback_data, button)`` without free text of the user."""
    if update.callback_query is not None:
        return "callback", update.callback_query.data, None
    if update.message is not None:
        text = update.message.text or update.message.caption or ""
        if update.message.text and update.message.text.split(maxsplit=1)[:1] == ["/start"]:
            return "command", None, "/start"
        return "message", None, TEXT_MARKER if text else None
    return update.event_type, None, None
