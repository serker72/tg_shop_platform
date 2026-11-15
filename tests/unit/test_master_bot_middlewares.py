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
    MAINTENANCE_BLOCKED_KEY,
    SNAPSHOT_KEY,
    TEXT_MARKER,
    AuthMiddleware,
    ClickLogMiddleware,
    MaintenanceMiddleware,
)
from app.entrypoints.master_bot.texts import Texts
from app.modules.bot_manager.sessions import build_bot_session
from app.modules.iam.snapshot import PermissionSnapshot
from app.modules.logging.events import ActivityEvent

pytestmark = pytest.mark.req("R-II.3", "S-II.2")

USER = User(id=421, is_bot=False, first_name="u", username="staff")
PRIVATE = Chat(id=USER.id, type="private")
GROUP = Chat(id=-100500, type="supergroup")
MISSING = PermissionSnapshot.missing()
SUPER = PermissionSnapshot(user_id=uuid4(), is_superuser=True)
NO_RIGHTS = PermissionSnapshot(user_id=uuid4())
VIEWER = PermissionSnapshot(user_id=uuid4(), role_permissions={"users.view": frozenset({"r"})})
SETTINGS_ADMIN = PermissionSnapshot(
    user_id=uuid4(), role_permissions={"settings.manage": frozenset({"admin"})}
)
BOT_TOKEN = "123456789:AAH-secretTokenValue_abcdefghijklmnop"


def _message_update(text: str = "/start", chat: Chat = PRIVATE) -> Update:
    message = Message(message_id=1, date=datetime.now(UTC), chat=chat, from_user=USER, text=text)
    return Update(update_id=1, message=message)


def _callback_update(data: str = "shops") -> Update:
    callback = CallbackQuery(id="cb1", from_user=USER, chat_instance="ci", data=data)
    return Update(update_id=2, callback_query=callback)


def _data(chat: Chat = PRIVATE, **extra: Any) -> dict[str, Any]:
    return {"event_from_user": USER, "event_chat": chat, **extra}


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
    data = _data(bot=bot)
    assert await AuthMiddleware(_loader(snapshot))(handler, _message_update(), data) is None
    assert handler.calls == []
    assert bot.mock_calls == []  # no answer at all


async def test_auth_ignores_non_private_chat_even_for_superuser() -> None:
    handler, bot, loader = _Handler(), AsyncMock(), _loader(SUPER)
    update = _message_update(chat=GROUP)
    assert await AuthMiddleware(loader)(handler, update, _data(GROUP, bot=bot)) is None
    assert handler.calls == [] and bot.mock_calls == []
    loader.assert_not_awaited()


@pytest.mark.parametrize("snapshot", [SUPER, VIEWER, SETTINGS_ADMIN])
async def test_auth_passes_staff_and_puts_snapshot(snapshot: PermissionSnapshot) -> None:
    handler = _Handler()
    assert await AuthMiddleware(_loader(snapshot))(handler, _message_update(), _data()) == "handled"
    assert handler.calls[0][SNAPSHOT_KEY] is snapshot


async def test_auth_ignores_update_without_user() -> None:
    handler = _Handler()
    assert await AuthMiddleware(_loader(SUPER))(handler, Update(update_id=3), {}) is None
    assert handler.calls == []


async def test_auth_syncs_username_only_when_changed() -> None:
    sync = AsyncMock()
    middleware = AuthMiddleware(_loader(SUPER), sync)
    await middleware(_Handler(), _message_update(), _data())
    await middleware(_Handler(), _message_update(), _data())
    sync.assert_awaited_once_with(USER.id, "staff")
    renamed = USER.model_copy(update={"username": "new_name"})
    await middleware(_Handler(), _message_update(), {**_data(), "event_from_user": renamed})
    assert sync.await_count == 2
    sync.assert_awaited_with(USER.id, "new_name")


async def test_auth_username_sync_failure_is_best_effort() -> None:
    sync = AsyncMock(side_effect=RuntimeError("db down"))
    handler = _Handler()
    middleware = AuthMiddleware(_loader(VIEWER), sync)
    assert await middleware(handler, _message_update(), _data()) == "handled"
    await middleware(handler, _message_update(), _data())
    assert sync.await_count == 2  # retried: the failed value was not remembered
    assert len(handler.calls) == 2


async def test_auth_no_username_sync_for_ignored_user() -> None:
    sync = AsyncMock()
    await AuthMiddleware(_loader(MISSING), sync)(_Handler(), _message_update(), _data())
    sync.assert_not_awaited()


async def test_maintenance_stub_for_regular_user() -> None:
    handler, bot = _Handler(), AsyncMock()
    data: dict[str, Any] = {SNAPSHOT_KEY: VIEWER, "bot": bot}
    assert await MaintenanceMiddleware(_Gate(True))(handler, _message_update(), data) is None
    assert handler.calls == [] and data[MAINTENANCE_BLOCKED_KEY] is True
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
    await middleware(handler, _callback_update("shops"), _data())
    await middleware(handler, _message_update("/start deep_link"), _data())
    first, second = activity.events
    assert first.telegram_bot_id == second.telegram_bot_id == 777000
    assert first.tenant_id is None and first.telegram_user_id == USER.id
    assert (first.action_type, first.callback_data, first.source) == (
        "callback",
        "shops",
        "master_bot",
    )
    assert (second.action_type, second.button, second.username) == ("command", "/start", "staff")
    assert first.result is None
    assert len(handler.calls) == 2


async def test_click_log_never_stores_free_text() -> None:
    activity = _Activity()
    middleware = ClickLogMiddleware(activity, telegram_bot_id=1)
    await middleware(_Handler(), _message_update(f"токен {BOT_TOKEN}"), _data())
    await middleware(_Handler(), _message_update(BOT_TOKEN), _data())
    for event in activity.events:
        assert event.action_type == "message" and event.button == TEXT_MARKER
        assert BOT_TOKEN not in repr(event) and "secretToken" not in repr(event)


async def test_click_log_marks_maintenance_blocked_update() -> None:
    activity, bot = _Activity(), AsyncMock()
    maintenance = MaintenanceMiddleware(_Gate(True))
    inner = _Handler()

    async def chain(event: Any, data: dict[str, Any]) -> Any:
        return await maintenance(inner, event, data)

    click = ClickLogMiddleware(activity, telegram_bot_id=1)
    await click(chain, _message_update(), {**_data(bot=bot), SNAPSHOT_KEY: VIEWER})
    await click(chain, _message_update(), {**_data(bot=bot), SNAPSHOT_KEY: SUPER})
    blocked, passed = activity.events
    assert blocked.result == "maintenance" and passed.result is None
    assert len(inner.calls) == 1


async def test_click_log_failure_does_not_break_update() -> None:
    class _Broken:
        def log(self, event: ActivityEvent) -> None:
            raise RuntimeError("boom")

    handler = _Handler()
    middleware = ClickLogMiddleware(_Broken(), telegram_bot_id=1)
    assert await middleware(handler, _message_update(), _data()) == "handled"


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
