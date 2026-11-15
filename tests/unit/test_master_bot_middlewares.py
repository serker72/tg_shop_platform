"""Master Bot middlewares, menu visibility, config token and proxy session (task 04)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.core.config import Settings
from app.entrypoints.master_bot.app import resolve_master_bot_proxy
from app.entrypoints.master_bot.menu import visible_sections
from app.entrypoints.master_bot.middlewares import (
    SNAPSHOT_KEY,
    AuthMiddleware,
    ClickLogMiddleware,
    MaintenanceMiddleware,
)
from app.entrypoints.master_bot.texts import Texts
from app.modules.bot_manager.sessions import build_bot_session
from app.modules.iam.snapshot import PermissionSnapshot
from app.modules.logging.events import ActivityEvent

pytestmark = pytest.mark.req("R-II.3")

USER = User(id=421, is_bot=False, first_name="u", username="staff")
MISSING = PermissionSnapshot.missing()
SUPER = PermissionSnapshot(user_id=uuid4(), is_superuser=True)
NO_RIGHTS = PermissionSnapshot(user_id=uuid4())
VIEWER = PermissionSnapshot(user_id=uuid4(), role_permissions={"users.view": frozenset({"r"})})
SETTINGS_ADMIN = PermissionSnapshot(
    user_id=uuid4(), role_permissions={"settings.manage": frozenset({"admin"})}
)


def _message_update(text: str = "/start") -> Update:
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=USER.id, type="private"),
        from_user=USER,
        text=text,
    )
    return Update(update_id=1, message=message)


def _callback_update(data: str = "shops") -> Update:
    callback = CallbackQuery(id="cb1", from_user=USER, chat_instance="ci", data=data)
    return Update(update_id=2, callback_query=callback)


class _Handler:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, event: Any, data: dict[str, Any]) -> str:
        self.calls.append(data)
        return "handled"


class _Gate:
    def __init__(self, on: bool) -> None:
        self.on = on

    async def is_on(self) -> bool:
        return self.on


class _Activity:
    def __init__(self) -> None:
        self.events: list[ActivityEvent] = []

    def log(self, event: ActivityEvent) -> None:
        self.events.append(event)


def _loader(snapshot: PermissionSnapshot) -> Any:
    return AsyncMock(return_value=snapshot)


@pytest.mark.parametrize("snapshot", [MISSING, NO_RIGHTS], ids=["unknown_inactive_deleted", "no"])
async def test_auth_ignores_silently(snapshot: PermissionSnapshot) -> None:
    handler, bot = _Handler(), AsyncMock()
    data: dict[str, Any] = {"event_from_user": USER, "bot": bot}
    assert await AuthMiddleware(_loader(snapshot))(handler, _message_update(), data) is None
    assert handler.calls == []
    assert bot.mock_calls == []  # no answer at all


@pytest.mark.parametrize("snapshot", [SUPER, VIEWER, SETTINGS_ADMIN])
async def test_auth_passes_staff_and_puts_snapshot(snapshot: PermissionSnapshot) -> None:
    handler = _Handler()
    data: dict[str, Any] = {"event_from_user": USER}
    assert await AuthMiddleware(_loader(snapshot))(handler, _message_update(), data) == "handled"
    assert handler.calls[0][SNAPSHOT_KEY] is snapshot


async def test_auth_ignores_update_without_user() -> None:
    handler = _Handler()
    assert await AuthMiddleware(_loader(SUPER))(handler, Update(update_id=3), {}) is None
    assert handler.calls == []


async def test_maintenance_stub_for_regular_user() -> None:
    handler, bot = _Handler(), AsyncMock()
    data: dict[str, Any] = {SNAPSHOT_KEY: VIEWER, "bot": bot}
    assert await MaintenanceMiddleware(_Gate(True))(handler, _message_update(), data) is None
    assert handler.calls == []
    bot.send_message.assert_awaited_once_with(USER.id, Texts.maintenance())


async def test_maintenance_stub_for_callback() -> None:
    bot = AsyncMock()
    data: dict[str, Any] = {SNAPSHOT_KEY: VIEWER, "bot": bot}
    await MaintenanceMiddleware(_Gate(True))(_Handler(), _callback_update(), data)
    bot.answer_callback_query.assert_awaited_once()


@pytest.mark.parametrize("snapshot", [SUPER, SETTINGS_ADMIN], ids=["superuser", "settings"])
async def test_maintenance_passes_superuser_and_settings_manage(
    snapshot: PermissionSnapshot,
) -> None:
    handler = _Handler()
    data: dict[str, Any] = {SNAPSHOT_KEY: snapshot, "bot": AsyncMock()}
    assert await MaintenanceMiddleware(_Gate(True))(handler, _message_update(), data) == "handled"


async def test_maintenance_off_passes_everyone() -> None:
    handler = _Handler()
    data: dict[str, Any] = {SNAPSHOT_KEY: VIEWER}
    assert await MaintenanceMiddleware(_Gate(False))(handler, _message_update(), data) == "handled"


async def test_click_log_uses_master_bot_telegram_id() -> None:
    activity, handler = _Activity(), _Handler()
    middleware = ClickLogMiddleware(activity, telegram_bot_id=777000)
    await middleware(handler, _callback_update("shops"), {"event_from_user": USER})
    await middleware(handler, _message_update("/start"), {"event_from_user": USER})
    first, second = activity.events
    assert first.telegram_bot_id == second.telegram_bot_id == 777000
    assert first.tenant_id is None and first.telegram_user_id == USER.id
    assert (first.action_type, first.callback_data, first.source) == (
        "callback",
        "shops",
        "master_bot",
    )
    assert second.action_type == "command" and second.username == "staff"
    assert len(handler.calls) == 2


async def test_click_log_failure_does_not_break_update() -> None:
    class _Broken:
        def log(self, event: ActivityEvent) -> None:
            raise RuntimeError("boom")

    handler = _Handler()
    middleware = ClickLogMiddleware(_Broken(), telegram_bot_id=1)
    assert await middleware(handler, _message_update(), {"event_from_user": USER}) == "handled"


def test_menu_sections_by_permissions() -> None:
    assert all(visible_sections(SUPER).values())
    viewer = visible_sections(VIEWER)
    assert viewer["staff"] and viewer["logs"] and not viewer["roles"] and not viewer["settings"]
    moderator = visible_sections(PermissionSnapshot(user_id=uuid4(), tenants=frozenset({uuid4()})))
    assert moderator["shops"] and moderator["bots"] and not moderator["staff"]


def test_master_bot_token_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MASTER_BOT_TOKEN", "")
    assert Settings(_env_file=None).telegram.master_bot_token is None
    monkeypatch.setenv("MASTER_BOT_TOKEN", "123456:ABCdef")
    token = Settings(_env_file=None).telegram.master_bot_token
    assert token is not None and token.get_secret_value() == "123456:ABCdef"
    assert "ABCdef" not in repr(Settings(_env_file=None).telegram)


@pytest.mark.req("R-II.14")
async def test_master_bot_session_uses_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MASTER_BOT_USE_PROXY", "true")
    monkeypatch.setenv("MASTER_BOT_PROXY_URL", "socks5://user:pass@10.0.0.1:1080")
    proxy = resolve_master_bot_proxy(Settings(_env_file=None))
    assert proxy == "socks5://user:pass@10.0.0.1:1080"
    session = build_bot_session(proxy)
    try:
        assert session.proxy is not None
    finally:
        await session.close()
    monkeypatch.setenv("MASTER_BOT_USE_PROXY", "false")
    assert resolve_master_bot_proxy(Settings(_env_file=None)) is None
