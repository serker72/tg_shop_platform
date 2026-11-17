"""Master Bot dialog "Settings" (task 07) via ``aiogram_dialog.test_tools``."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.modules.iam.permissions import SETTINGS_MANAGE
from app.modules.iam.snapshot import PermissionSnapshot
from app.modules.system.presentation.tg_master.settings import FIELDS, parse_value
from app.modules.system.service import RETENTION_GUARD
from app.shared_kernel.domain.exceptions import DomainError
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


async def test_guard_error_shows_message() -> None:
    env = Env(SETTINGS_ADMIN)
    env.system.errors["set_retention"] = DomainError("guard", code=RETENTION_GUARD)
    await _open(env)
    await env.press("Хранение user_activity (мес.)")
    await env.send("6")
    assert "pg_partman" in env.text()
    assert env.system_settings.user_activity_retention_months == 3
