"""Master Bot RBAC dialogs via ``aiogram_dialog.test_tools`` (task 06): roles.

The environment (fake services, read queries, forged callbacks) is shared with the shops
and staff dialogs: :mod:`tests.unit._master_bot_env`.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.modules.iam.permissions import (
    LOGS_VIEW,
    PRODUCTS_CREATE,
    PRODUCTS_VIEW,
    ROLES_MANAGE,
    SEED_PERMISSIONS,
)
from app.modules.iam.presentation.tg_master.common import error_text
from app.modules.iam.presentation.tg_master.roles import (
    MODULES,
    PERMISSION_NAMES,
    module_of,
    module_titles,
    parse_role_slug,
)
from app.modules.iam.snapshot import PermissionSnapshot
from app.shared_kernel.domain.exceptions import DomainError
from tests.unit._master_bot_env import ACTOR, R4, SCENARIO, SUPER, VIEWER, Env, make_role

#: Holds roles.manage only (enough for the whole roles section).
ROLES_ADMIN = PermissionSnapshot(
    user_id=uuid4(), role_permissions={ROLES_MANAGE: frozenset({"admin"})}
)


async def _open_card(env: Env, label: str) -> None:
    await env.start()
    await env.press("Роли")
    await env.press(label, regex=True)


# --- catalogue ---------------------------------------------------------------------------


def test_permission_modules_cover_catalogue() -> None:
    assert len(PERMISSION_NAMES) == 32
    assert sum(len(slugs) for slugs in MODULES.values()) == len(SEED_PERMISSIONS)
    assert set(MODULES) <= set(module_titles())
    assert all(module_of(s) == m for m, slugs in MODULES.items() for s in slugs)


def test_role_slug_parser_matches_service() -> None:
    assert parse_role_slug("  support_2 ") == "support_2"
    for bad in ("", "Support", "2nd", "with-dash", "a b"):
        with pytest.raises(ValueError):
            parse_role_slug(bad)


# --- section entry -----------------------------------------------------------------------


@R4
async def test_roles_section_needs_roles_manage() -> None:
    env = Env(VIEWER, roles=[make_role("Custom", "custom")])
    await env.start()
    assert "Роли" not in env.buttons()
    await env.forge("logs", "logs", "roles")
    assert "roles" not in env.queries.calls


@SCENARIO
async def test_roles_list_marks_system_and_inactive() -> None:
    env = Env(
        ROLES_ADMIN,
        roles=[make_role("Admin", "admin", system=True), make_role("Old", "old", active=False)],
    )
    await env.start()
    await env.press("Роли")
    assert "Роли: 2" in env.text()
    assert "🔒 Admin (admin)" in env.buttons() and "Old (old) ⏸" in env.buttons()


# --- create / rename ---------------------------------------------------------------------


@SCENARIO
async def test_create_role_opens_card() -> None:
    env = Env(ROLES_ADMIN)
    await env.start()
    await env.press("Роли")
    await env.press("Создать роль")
    await env.send("Поддержка")
    await env.send("Bad-Slug")  # rejected by TextInput, no service call
    assert error_text("invalid_role_slug") in env.text()
    assert env.roles.calls == []
    await env.send("support")
    assert ("create", ACTOR, "Поддержка", "support") in env.roles.calls
    assert "Готово." in env.text()
    assert "Роль: Поддержка" in env.text() and "Slug: support" in env.text()


@R4
async def test_create_role_slug_taken_is_localized() -> None:
    env = Env(ROLES_ADMIN)
    env.roles.errors["create"] = DomainError("taken", code="role_slug_taken")
    await env.start()
    await env.press("Роли")
    await env.press("Создать роль")
    await env.send("Поддержка")
    await env.send("support")
    assert error_text("role_slug_taken") in env.text()


@SCENARIO
async def test_rename_role() -> None:
    role = make_role("Custom", "custom")
    env = Env(ROLES_ADMIN, roles=[role])
    await _open_card(env, "Custom \\(custom\\)")
    await env.press("Переименовать")
    await env.send("Renamed")
    assert ("rename", ACTOR, role.id, "Renamed") in env.roles.calls
    assert "Роль: Renamed" in env.text()


# --- permissions multiselect ---------------------------------------------------------------


@SCENARIO
async def test_multiselect_saves_exact_slugs() -> None:
    role = make_role("Custom", "custom")
    env = Env(ROLES_ADMIN, roles=[role])
    env.role_perms[role.id] = frozenset({PRODUCTS_VIEW, LOGS_VIEW})
    await _open_card(env, "Custom \\(custom\\)")
    await env.press("Права роли")
    assert "отмечено 2 из 32" in env.text()
    await env.press("✅ Просмотр товаров")  # uncheck
    await env.press("▫️ Создание товаров")  # check
    await env.press(".*Роли 0/1", regex=True)  # switch module, selection is kept
    assert "Модуль: Роли" in env.text()
    await env.press("▫️ Управление ролями")
    await env.press("Сохранить")
    assert env.roles.calls == [
        ("set_permissions", ACTOR, role.id, frozenset({PRODUCTS_CREATE, LOGS_VIEW, ROLES_MANAGE}))
    ]
    assert "Готово." in env.text() and "Роль: Custom" in env.text()


@R4
@pytest.mark.parametrize("code", ["privilege_escalation", "system_role_protected"])
async def test_set_permissions_error_is_localized(code: str) -> None:
    role = make_role("Custom", "custom")
    env = Env(ROLES_ADMIN, roles=[role])
    env.roles.errors["set_permissions"] = DomainError("no", code=code)
    await _open_card(env, "Custom \\(custom\\)")
    await env.press("Права роли")
    await env.press("▫️ Создание товаров")
    await env.press("Сохранить")
    assert error_text(code) in env.text()
    assert "Модуль:" in env.text()  # stays on the editor


@SCENARIO
async def test_system_role_is_read_only() -> None:
    role = make_role("Admin", "admin", system=True)
    env = Env(ROLES_ADMIN, roles=[role])
    env.role_perms[role.id] = frozenset({PRODUCTS_VIEW, ROLES_MANAGE})
    await _open_card(env, "🔒 Admin \\(admin\\)")
    assert "Системная роль" in env.text()
    buttons = env.buttons()
    assert "Права роли" in buttons and "Переименовать" in buttons
    assert not {"Деактивировать", "Активировать", "Удалить"} & set(buttons)
    await env.press("Права роли")
    assert "только просмотр" in env.text()
    assert "Просмотр товаров" in env.text() and "Управление ролями" in env.text()
    assert "Сохранить" not in env.buttons()
    assert not any(b.startswith(("✅", "▫️")) for b in env.buttons())


@R4
async def test_forged_save_of_system_role_does_not_call_service() -> None:
    system = make_role("Admin", "admin", system=True)
    custom = make_role("Custom", "custom")
    env = Env(ROLES_ADMIN, roles=[system, custom])
    await _open_card(env, "Custom \\(custom\\)")
    await env.press("Права роли")
    await env.press("« Назад")
    await env.press("« Назад")
    await env.press("🔒 Admin \\(admin\\)", regex=True)
    await env.press("Права роли")
    await env.forge("r_perms_back", "r_perms_back", "r_save")
    assert env.roles.calls == []
    assert error_text("system_role_protected") in env.text()


# --- status and delete -------------------------------------------------------------------


@SCENARIO
async def test_deactivate_activate_and_delete_role() -> None:
    role = make_role("Custom", "custom")
    env = Env(ROLES_ADMIN, roles=[role])
    await _open_card(env, "Custom \\(custom\\)")
    await env.press("Деактивировать")
    await env.press("Да")
    assert ("deactivate", ACTOR, role.id) in env.roles.calls
    assert "деактивирована" in env.text()
    await env.press("Активировать")
    assert ("activate", ACTOR, role.id) in env.roles.calls
    await env.press("Удалить")
    await env.press("Да")
    assert ("soft_delete", ACTOR, role.id) in env.roles.calls
    assert "Роли: 0" in env.text()


# --- forged callbacks without roles.manage -------------------------------------------------


@R4
async def test_forged_role_card_and_save_without_right_show_nothing() -> None:
    role = make_role("Secret role", "secret")
    env = Env(SUPER, roles=[role])
    env.role_perms[role.id] = frozenset({PRODUCTS_VIEW})
    await _open_card(env, "Secret role \\(secret\\)")
    await env.press("Права роли")
    env.snapshot = VIEWER  # the right was revoked (or the client is forged)
    env.queries.calls.clear()
    await env.forge("r_perms_back", "r_perms_back", "r_save")
    assert env.roles.calls == []
    assert not {"role", "role_permission_slugs", "roles"} & set(env.queries.calls)
    env.snapshot = SUPER
    await env.start()
    await env.press("Роли")
    env.snapshot = VIEWER
    env.queries.calls.clear()
    await env.forge("r_sel", "r_sel", "r_sel")  # card of a role, right revoked
    assert "Secret role" not in env.text()
    assert not {"role", "role_permission_slugs", "roles"} & set(env.queries.calls)
    assert env.roles.calls == []
