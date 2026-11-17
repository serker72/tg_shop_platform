"""Master Bot dialog "Settings" (task 07) via ``aiogram_dialog.test_tools``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from dishka.integrations.aiogram import CONTAINER_NAME

from app.entrypoints.master_bot.middlewares import SNAPSHOT_KEY
from app.modules.iam.permissions import SETTINGS_MANAGE
from app.modules.iam.presentation.tg_master.common import NOTICE_KEY
from app.modules.iam.snapshot import PermissionSnapshot
from app.modules.system.presentation.tg_master.settings import (
    FIELDS,
    RETENTION_KEY,
    SettingsSG,
    _on_confirm,
    _on_retention_confirm,
    parse_value,
)
from app.modules.system.service import RETENTION_CONFIG_ERROR, RETENTION_GUARD, SETTINGS_BUSY
from app.shared_kernel.domain.exceptions import DomainError, EntityNotFoundError
from tests.unit._master_bot_env import ACTOR, SUPER, VIEWER, Env

pytestmark = pytest.mark.req("R-II.5")

SETTINGS_ADMIN = PermissionSnapshot(
    user_id=uuid4(), role_permissions={SETTINGS_MANAGE: frozenset({"admin"})}
)


async def _open(env: Env) -> None:
    await env.start()
    await env.press("Настройки")


def test_parse_value_bounds() -> None:
    assert parse_value("ua", "1") == 1
    assert parse_value("ua", " 120 ") == 120
    assert parse_value("ua", "0") is None
    assert parse_value("ua", "121") is None
    assert parse_value("files", "3650") == 3650
    assert parse_value("files", "3651") is None
    assert parse_value("ttl", "abc") is None
    assert parse_value("ttl", None) is None
    assert set(FIELDS) == {"ttl", "ua", "audit", "files"}


@pytest.mark.parametrize("raw", ["1_000", "+5", "-5", "٣", "１２", "1.0", "", "1 2"])
def test_parse_value_ascii_digits_only(raw: str) -> None:
    assert parse_value("files", raw) is None


async def test_confirm_without_target_does_not_call_service() -> None:
    container = AsyncMock()
    manager = SimpleNamespace(
        dialog_data={},
        middleware_data={
            SNAPSHOT_KEY: SUPER,
            "event_from_user": SimpleNamespace(id=ACTOR),
            CONTAINER_NAME: container,
        },
        switch_to=AsyncMock(),
    )
    await _on_confirm(AsyncMock(), cast(Any, None), cast(Any, manager))
    container.get.assert_not_called()
    manager.switch_to.assert_awaited_once_with(SettingsSG.main)
    assert "Некорректное действие" in manager.dialog_data[NOTICE_KEY]


async def test_missing_settings_row_shows_error() -> None:
    env = Env(SETTINGS_ADMIN)
    env.system.errors["get"] = EntityNotFoundError("missing", code="system_settings_missing")
    await _open(env)
    assert "не инициализированы" in env.text()
    assert "Включить техработы" not in env.buttons()


@pytest.mark.parametrize(
    ("code", "needle"),
    [(RETENTION_CONFIG_ERROR, "не найдена или некорректна"), (SETTINGS_BUSY, "другим процессом")],
)
async def test_config_and_busy_errors_have_own_text(code: str, needle: str) -> None:
    env = Env(SETTINGS_ADMIN)
    env.system.errors["set_retention"] = DomainError("x", code=code)
    await _open(env)
    await env.press("Хранение user_activity (мес.)")
    await env.send("6")
    assert needle in env.text()
    assert "архивирование партиций" not in env.text()


async def test_stale_permission_cache_warning() -> None:
    env = Env(SETTINGS_ADMIN)
    env.system.cache_invalidated = False
    await _open(env)
    await env.press("TTL кэша прав (сек.)")
    await env.send("600")
    assert "кэш прав обновится с задержкой" in env.text()


async def test_button_only_with_settings_manage() -> None:
    env = Env(VIEWER)
    await env.start()
    assert "Настройки" not in env.buttons()
    env = Env(SETTINGS_ADMIN)
    await env.start()
    assert "Настройки" in env.buttons()
    await env.press("Настройки")
    assert "Системные настройки" in env.text()
    assert "Технические работы: выключены" in env.text()


async def test_forged_section_callback_rejected() -> None:
    env = Env(VIEWER)
    await env.start()
    await env.forge("logs", "logs", "settings")
    assert "Системные настройки" not in env.text()
    assert env.system.calls == []


async def test_forged_action_after_right_revoked_rejected() -> None:
    env = Env(SETTINGS_ADMIN)
    await _open(env)
    env.snapshot = VIEWER
    await env.forge("mnt", "mnt", "mnt")
    assert all(call[0] == "get" for call in env.system.calls)
    assert env.system_settings.is_maintenance_mode is False


async def test_maintenance_requires_confirmation() -> None:
    env = Env(SUPER)
    await _open(env)
    await env.press("Включить техработы")
    assert "Включить режим технических работ?" in env.text()
    await env.press("Нет")
    assert env.system_settings.is_maintenance_mode is False
    await env.press("Включить техработы")
    await env.press("Да")
    assert ("set_maintenance", ACTOR, True) in env.system.calls
    assert "Готово." in env.text()
    assert "Технические работы: включены" in env.text()
    assert "Выключить техработы" in env.buttons()


async def test_invalid_input_keeps_value() -> None:
    env = Env(SETTINGS_ADMIN)
    await _open(env)
    await env.press("Хранение audit_logs (мес.)")
    await env.send("121")
    assert "от 1 до 120" in env.text()
    await env.send("abc")
    assert "Некорректное значение" in env.text()
    assert not any(call[0] == "set_retention" for call in env.system.calls)
    await env.send("36")
    assert ("set_retention", ACTOR, 36) in env.system.calls  # only the edited column
    assert "Хранение audit_logs (мес.): 36" in env.text()


async def test_ttl_and_file_logs_input() -> None:
    env = Env(SETTINGS_ADMIN)
    await _open(env)
    await env.press("TTL кэша прав (сек.)")
    await env.send("600")
    assert ("set_iam_cache_ttl", ACTOR, 600) in env.system.calls
    await env.press("Хранение файловых логов (дн.)")
    await env.send("3651")
    assert not any(c[0] == "set_file_logs_retention_days" for c in env.system.calls)
    await env.send("30")
    assert ("set_file_logs_retention_days", ACTOR, 30) in env.system.calls


def _retention_calls(env: Env) -> list[tuple[Any, ...]]:
    return [call for call in env.system.calls if call[0] == "set_retention"]


async def test_retention_decrease_requires_confirmation() -> None:
    env = Env(SETTINGS_ADMIN)
    await _open(env)
    await env.press("Хранение audit_logs (мес.)")
    await env.send("12")
    assert "audit_logs старше 12 мес. будут отсоединены и архивированы" in env.text()
    assert _retention_calls(env) == []
    await env.press("Да")
    assert ("set_retention", ACTOR, 12) in env.system.calls
    assert "Хранение audit_logs (мес.): 12" in env.text()
    await env.press("Хранение user_activity (мес.)")
    await env.send("2")
    assert "user_activity старше 2 мес. будут удалены безвозвратно" in env.text()
    await env.press("Да")
    assert ("set_retention", ACTOR, 2) in env.system.calls


async def test_retention_decrease_cancelled() -> None:
    env = Env(SETTINGS_ADMIN)
    await _open(env)
    await env.press("Хранение user_activity (мес.)")
    await env.send("1")
    assert "будут удалены безвозвратно" in env.text()
    await env.press("Нет")
    assert _retention_calls(env) == []
    assert env.system_settings.user_activity_retention_months == 3
    assert "Системные настройки" in env.text()


async def test_retention_increase_or_equal_without_confirmation() -> None:
    env = Env(SETTINGS_ADMIN)
    await _open(env)
    await env.press("Хранение user_activity (мес.)")
    await env.send("3")  # equal
    assert "безвозвратно" not in env.text()
    assert ("set_retention", ACTOR, 3) in env.system.calls
    await env.press("Хранение audit_logs (мес.)")
    await env.send("36")  # increase
    assert "архивированы" not in env.text()
    assert ("set_retention", ACTOR, 36) in env.system.calls


@pytest.mark.parametrize(
    "pending",
    [
        None,
        {"field": "ua"},
        {"field": "ua", "value": "2"},
        {"field": "ttl", "value": 2},
        {"field": "ua", "value": True},
        {"field": "ua", "value": 0},
        "ua:2",
    ],
)
async def test_forged_retention_yes_does_not_call_service(pending: Any) -> None:
    container = AsyncMock()
    data: dict[str, Any] = {} if pending is None else {RETENTION_KEY: pending}
    manager = SimpleNamespace(
        dialog_data=data,
        middleware_data={
            SNAPSHOT_KEY: SUPER,
            "event_from_user": SimpleNamespace(id=ACTOR),
            CONTAINER_NAME: container,
        },
        switch_to=AsyncMock(),
    )
    await _on_retention_confirm(AsyncMock(), cast(Any, None), cast(Any, manager))
    container.get.assert_not_called()
    manager.switch_to.assert_awaited_once_with(SettingsSG.main)
    assert "Некорректное действие" in manager.dialog_data[NOTICE_KEY]
    assert RETENTION_KEY not in manager.dialog_data


async def test_retention_yes_rechecks_permission() -> None:
    env = Env(SETTINGS_ADMIN)
    await _open(env)
    await env.press("Хранение user_activity (мес.)")
    await env.send("1")
    env.snapshot = VIEWER
    await env.forge("ret_yes", "ret_yes", "ret_yes")
    assert _retention_calls(env) == []


async def test_guard_error_shows_message() -> None:
    env = Env(SETTINGS_ADMIN)
    env.system.errors["set_retention"] = DomainError("guard", code=RETENTION_GUARD)
    await _open(env)
    await env.press("Хранение user_activity (мес.)")
    await env.send("6")
    assert "pg_partman" in env.text()
    assert env.system_settings.user_activity_retention_months == 3
