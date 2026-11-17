"""Master Bot RBAC dialogs via ``aiogram_dialog.test_tools`` (task 06): roles.

The environment (fake services, read queries, forged callbacks) is shared with the shops
and staff dialogs: :mod:`tests.unit._master_bot_env`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from aiogram_dialog.widgets.kbd.calendar_kbd import raw_from_date

from app.modules.iam.effective_permissions import (
    EffectivePermission,
    EffectivePermissionsReport,
    OverrideRef,
    RoleRef,
    UserStatus,
)
from app.modules.iam.permissions import (
    ACCOUNTING_MANAGE,
    LOGS_VIEW,
    PRODUCTS_CREATE,
    PRODUCTS_VIEW,
    ROLES_MANAGE,
    SEED_PERMISSION_SLUGS,
    SEED_PERMISSIONS,
    USERS_VIEW,
)
from app.modules.iam.presentation.tg_master.effective import PAGE_LIMIT, paginate
from app.modules.iam.presentation.tg_master.overrides import end_of_day, parse_reason
from app.modules.iam.presentation.tg_master.queries import OverrideRow
from app.modules.iam.presentation.tg_master.roles import (
    MODULES,
    PERMISSION_NAMES,
    module_of,
    module_titles,
    parse_role_slug,
)
from app.modules.iam.snapshot import DecisionSource, PermissionSnapshot
from app.shared_kernel.domain.exceptions import DomainError
from app.shared_kernel.presentation.tg_master.common import (
    error_text,
)
from tests.unit._master_bot_env import (
    ACTOR,
    R4,
    SCENARIO,
    STAFF_READER,
    SUPER,
    VIEWER,
    Env,
    _tenant,
    _user,
    make_role,
)

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


# --- staff roles (part 2) ------------------------------------------------------------------

STAFF = 321


async def _staff_card(env: Env) -> None:
    await env.start()
    await env.press("Сотрудники")
    await env.press(f"— \\({STAFF}\\)", regex=True)


@SCENARIO
async def test_assign_and_revoke_staff_role() -> None:
    staff, role = _user(STAFF), make_role("Custom", "custom")
    env = Env(SUPER, users=[staff], roles=[role])
    env.access.handlers["assign_role"] = lambda *a: env.user_roles.setdefault(staff.id, {role.id})
    env.access.handlers["revoke_role"] = lambda *a: env.user_roles.pop(staff.id)
    await _staff_card(env)
    await env.press("Роли сотрудника")
    await env.press("▫️ Custom (custom)")
    assert ("assign_role", ACTOR, STAFF, role.id) in env.access.calls
    assert "Готово." in env.text() and "✅ Custom (custom)" in env.buttons()
    await env.press("✅ Custom (custom)")
    assert "Снять роль «Custom» с сотрудника?" in env.text()
    assert not any(call[0] == "revoke_role" for call in env.access.calls)
    await env.press("Да")
    assert ("revoke_role", ACTOR, STAFF, role.id) in env.access.calls
    assert "▫️ Custom (custom)" in env.buttons()


@SCENARIO
async def test_assign_global_role_reports_dropped_shops() -> None:
    staff, role, shop = _user(STAFF), make_role("Senior", "senior"), _tenant("Shop A", "shop-a")
    env = Env(SUPER, [shop], users=[staff], roles=[role])
    env.moderated[staff.id] = [shop]
    env.access.handlers["assign_role"] = lambda *a: env.moderated.update({staff.id: []})
    await _staff_card(env)
    await env.press("Роли сотрудника")
    await env.press("▫️ Senior (senior)")
    assert "Готово." in env.text()
    assert "Привязки к магазинам сняты" in env.text()


@R4
async def test_own_card_has_no_roles_and_overrides_buttons() -> None:
    env = Env(SUPER, users=[_user(ACTOR)])
    await env.start()
    await env.press("Сотрудники")
    await env.press(f"— \\({ACTOR}\\)", regex=True)
    assert not {"Роли сотрудника", "Персональные права"} & set(env.buttons())


@R4
async def test_forged_staff_roles_and_overrides_without_roles_manage() -> None:
    env = Env(STAFF_READER, users=[_user(STAFF)], roles=[make_role("Custom", "custom")])
    await _staff_card(env)
    assert not {"Роли сотрудника", "Персональные права"} & set(env.buttons())
    env.queries.calls.clear()
    await env.forge("u_card_back", "u_card_back", "u_ovr")
    assert "user_overrides" not in env.queries.calls
    await env.forge("u_card_back", "u_card_back", "u_roles")
    assert "Нет доступа" in env.text() and "Custom" not in env.text()
    assert "user_role_ids" not in env.queries.calls
    assert env.access.calls == []


# --- personal overrides (part 2) ---------------------------------------------------------


def _row(slug: str, *, granted: bool, expired: bool = False, tenant: Any = None) -> OverrideRow:
    when = datetime.now(UTC) + timedelta(days=-1 if expired else 3)
    override = SimpleNamespace(
        id=uuid4(), tenant_id=tenant, is_granted=granted, expires_at=when, reason="аудит"
    )
    return OverrideRow(
        override=cast(Any, override),
        permission_slug=slug,
        granted_by_telegram_id=777,
        granted_by_username="boss",
        is_expired=expired,
    )


async def _overrides(env: Env) -> None:
    await _staff_card(env)
    await env.press("Персональные права")


async def _global_override(env: Env) -> None:
    await _overrides(env)
    await env.press("Добавить персональное право")
    await env.press("Роли")
    await env.press("Управление ролями")
    await env.press("Разрешить")


@SCENARIO
async def test_global_override_skips_shop_and_permanent() -> None:
    env = Env(SUPER, [_tenant("Shop A", "shop-a")], users=[_user(STAFF)])
    await _global_override(env)
    assert "Выберите последний день" in env.text()
    assert "Все магазины" not in env.buttons() and "Shop A (shop-a)" not in env.buttons()
    await env.press("Бессрочно")
    await env.press("Пропустить")
    await env.press("Сохранить")
    assert env.access.calls == [
        ("upsert_override", ACTOR, STAFF, ROLES_MANAGE, True, None, None, None)
    ]
    assert "Готово." in env.text() and "Действующие:" in env.text()


@SCENARIO
async def test_tenant_override_in_scope_with_date_and_reason() -> None:
    own, foreign = _tenant("Shop A", "shop-a"), _tenant("Shop B", "shop-b")
    snapshot = PermissionSnapshot(
        user_id=uuid4(),
        role_permissions={ROLES_MANAGE: frozenset({"r"}), USERS_VIEW: frozenset({"r"})},
        tenants=frozenset({own.id}),
    )
    env = Env(snapshot, [own, foreign], visible=[own], users=[_user(STAFF)])
    await _overrides(env)
    await env.press("Добавить персональное право")
    await env.press("Товары")
    await env.press("Просмотр товаров")
    await env.press("Запретить")
    assert "Все магазины" in env.buttons() and "Shop A (shop-a)" in env.buttons()
    assert "Shop B (shop-b)" not in env.buttons()
    await env.forge(str(own.id), str(own.id), str(foreign.id))
    assert error_text("tenant_not_found") in env.text()
    await env.press("Shop A (shop-a)")
    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    assert str(raw_from_date(yesterday)) not in str(env.messages.last_message().reply_markup)
    raw = str(raw_from_date(today))
    await env.forge(raw, raw, str(raw_from_date(yesterday)))
    assert error_text("expires_at_not_in_future") in env.text()
    assert env.access.calls == []
    await env.forge(raw, raw, raw)
    await env.send("временный запрет")
    await env.press("Сохранить")
    assert env.access.calls == [
        (
            "upsert_override",
            ACTOR,
            STAFF,
            PRODUCTS_VIEW,
            False,
            own.id,
            end_of_day(today),
            "временный запрет",
        )
    ]


@SCENARIO
async def test_overrides_list_split_edit_and_delete() -> None:
    staff = _user(STAFF)
    env = Env(SUPER, users=[staff])
    env.overrides[staff.id] = [
        _row(ROLES_MANAGE, granted=True),
        _row(PRODUCTS_VIEW, granted=False, expired=True),
    ]
    await _overrides(env)
    text = env.text()
    active, expired = text.index("Действующие:"), text.index("Истёкшие:")
    assert active < text.index("Управление ролями") < expired < text.index("Просмотр товаров")
    assert "@boss (777)" in text and "аудит" in text
    await env.press("Управление ролями · все магазины")
    await env.press("Изменить")
    await env.press("Запретить")
    assert "Все магазины" not in env.buttons()  # the slot is fixed
    await env.press("Бессрочно")
    await env.press("Пропустить")
    await env.press("Сохранить")
    assert ("upsert_override", ACTOR, STAFF, ROLES_MANAGE, False, None, None, None) in (
        env.access.calls
    )
    await env.press("⌛ Просмотр товаров · все магазины")
    await env.press("Удалить")
    await env.press("Да")
    assert ("remove_override", ACTOR, STAFF, PRODUCTS_VIEW, None) in env.access.calls


@R4
@pytest.mark.parametrize(
    "code",
    [
        "self_modification_forbidden",
        "privilege_escalation",
        "global_permission_tenant_override",
        "expires_at_not_in_future",
    ],
)
async def test_override_service_error_is_localized(code: str) -> None:
    env = Env(SUPER, users=[_user(STAFF)])
    env.access.errors["upsert_override"] = DomainError("no", code=code)
    await _global_override(env)
    await env.press("Бессрочно")
    await env.press("Пропустить")
    await env.press("Сохранить")
    assert error_text(code) in env.text()


@R4
@pytest.mark.parametrize("button", ["ov_sel", "ov_add"])
async def test_forged_override_callbacks_without_roles_manage(button: str) -> None:
    staff = _user(STAFF)
    env = Env(SUPER, users=[staff])
    env.overrides[staff.id] = [_row(ROLES_MANAGE, granted=True)]
    await _overrides(env)
    env.snapshot = STAFF_READER
    env.queries.calls.clear()
    await env.forge(button, button, button)
    assert "user_overrides" not in env.queries.calls
    assert "аудит" not in env.text()
    assert env.access.calls == []


def test_override_helpers() -> None:
    day = datetime(2030, 5, 17, tzinfo=UTC).date()
    assert end_of_day(day) == datetime(2030, 5, 17, 23, 59, 59, tzinfo=UTC)
    assert parse_reason("  ok ") == "ok"
    for bad in ("", "   ", "x" * 501):
        with pytest.raises(ValueError):
            parse_reason(bad)


# --- effective permissions (part 3) ------------------------------------------------------


def _entry(
    slug: str,
    source: DecisionSource,
    *,
    allowed: bool = True,
    tenant: Any = None,
    roles: tuple[RoleRef, ...] = (),
    override: OverrideRef | None = None,
) -> EffectivePermission:
    return EffectivePermission(
        permission_slug=slug,
        tenant_id=tenant,
        allowed=allowed,
        source=source,
        roles=roles,
        override=override,
    )


def _report(
    *entries: EffectivePermission,
    tenants: tuple[Any, ...] = (),
    expired: tuple[EffectivePermission, ...] = (),
    superuser: bool = False,
) -> EffectivePermissionsReport:
    return EffectivePermissionsReport(
        telegram_user_id=STAFF,
        status=UserStatus.ACTIVE,
        is_superuser=superuser,
        roles=(RoleRef("support", "Поддержка"),),
        tenants=tenants,
        permissions=entries,
        expired_overrides=expired,
    )


async def _effective(env: Env) -> None:
    await _staff_card(env)
    await env.press("Фактические права")


@SCENARIO
async def test_effective_shows_role_and_override_sources() -> None:
    boss = _user(777)
    boss.telegram_user_name = "boss"
    env = Env(STAFF_READER, users=[_user(STAFF), boss])
    until = datetime(2030, 1, 2, 23, 59, 59, tzinfo=UTC)
    ref = OverrideRef(
        is_granted=False,
        tenant_id=None,
        expires_at=until,
        granted_by_user_id=boss.id,
        reason="аудит",
    )
    gone = OverrideRef(
        is_granted=True, tenant_id=None, expires_at=until, granted_by_user_id=None, reason=None
    )
    env.reports[STAFF] = _report(
        _entry(PRODUCTS_VIEW, DecisionSource.ROLE, roles=(RoleRef("support", "Поддержка"),)),
        _entry(LOGS_VIEW, DecisionSource.OVERRIDE_DENY_GLOBAL, allowed=False, override=ref),
        expired=(_entry(ROLES_MANAGE, DecisionSource.NOT_GRANTED, override=gone),),
    )
    await _effective(env)
    text = env.text()
    assert "Статус: активен" in text
    assert "✅ Просмотр товаров — роль: Поддержка" in text
    assert "⛔ Просмотр логов — персонально: запрещено" in text
    assert "до 2030-01-02 23:59 UTC" in text and "@boss (777)" in text and "аудит" in text
    assert text.index("Истёкшие") < text.index("⌛ Управление ролями")


@SCENARIO
async def test_effective_shop_in_scope_and_forged_foreign_shop() -> None:
    own, foreign = _tenant("Shop A", "shop-a"), _tenant("Shop B", "shop-b")
    snapshot = PermissionSnapshot(
        user_id=uuid4(),
        role_permissions={USERS_VIEW: frozenset({"r"})},
        tenants=frozenset({own.id}),
    )
    env = Env(snapshot, [own, foreign], visible=[own], users=[_user(STAFF)])
    env.reports[STAFF] = _report(
        _entry(PRODUCTS_VIEW, DecisionSource.ROLE, roles=(RoleRef("support", "Поддержка"),)),
        _entry(
            PRODUCTS_VIEW,
            DecisionSource.NO_TENANT_ACCESS,
            allowed=False,
            tenant=foreign.id,
        ),
        _entry(PRODUCTS_VIEW, DecisionSource.ROLE, tenant=own.id, roles=(RoleRef("s", "Sup"),)),
        tenants=(own.id, foreign.id),
    )
    await _effective(env)
    await env.press("Права в магазине")
    assert "Shop A (shop-a)" in env.buttons() and "Shop B (shop-b)" not in env.buttons()
    await env.forge(str(own.id), str(own.id), str(foreign.id))
    assert error_text("tenant_not_found") in env.text()
    assert "Shop B" not in env.text()
    await env.press("Shop A (shop-a)")
    assert "Магазин: Shop A (shop-a)" in env.text()
    assert "✅ Просмотр товаров — роль: Sup" in env.text()
    assert "нет доступа к магазину" not in env.text()


@SCENARIO
async def test_effective_superuser_has_all_permissions() -> None:
    env = Env(SUPER, users=[_user(STAFF)])
    env.reports[STAFF] = _report(_entry("*", DecisionSource.SUPERUSER), superuser=True)
    await _effective(env)
    assert "Все права (суперпользователь)" in env.text()


@R4
async def test_forged_effective_without_users_view_shows_nothing() -> None:
    env = Env(SUPER, users=[_user(STAFF)])
    env.reports[STAFF] = _report(_entry(PRODUCTS_VIEW, DecisionSource.ROLE))
    await _staff_card(env)
    env.snapshot = ROLES_ADMIN  # roles.manage without users.view
    await env.forge("u_card_back", "u_card_back", "u_eff")
    assert env.permissions.calls == []
    await env.start()
    env.snapshot = SUPER
    await _effective(env)
    env.snapshot = ROLES_ADMIN
    env.permissions.calls.clear()
    await env.forge("ef_shops", "ef_shops", "ef_shops")
    assert env.permissions.calls == []
    assert "Просмотр товаров" not in env.text()


@SCENARIO
async def test_effective_long_report_is_paged_below_telegram_limit() -> None:
    env = Env(SUPER, users=[_user(STAFF)])
    ref = OverrideRef(
        is_granted=True, tenant_id=None, expires_at=None, granted_by_user_id=None, reason="x" * 400
    )
    entries = tuple(
        _entry(slug, DecisionSource.OVERRIDE_ALLOW_GLOBAL, override=ref)
        for slug, _name in SEED_PERMISSIONS
    )
    env.reports[STAFF] = _report(*entries)
    await _effective(env)
    seen = [env.text()]
    assert "Страница 1 из" in seen[0]
    while "›" in env.buttons():
        await env.press("›")
        seen.append(env.text())
    assert len(seen) > 1 and all(len(page) < 4096 for page in seen)
    assert sum(page.count("✅") for page in seen) == len(SEED_PERMISSIONS)
    await env.press("‹")
    assert f"Страница {len(seen) - 1} из {len(seen)}" in env.text()


def test_paginate_limits() -> None:
    pages = paginate(["a" * 10] * 5, limit=25)
    assert pages == ["a" * 10 + "\n" + "a" * 10] * 2 + ["a" * 10]
    assert paginate([]) == [""]
    assert len(paginate(["b" * (PAGE_LIMIT + 50)])[0]) == PAGE_LIMIT


# --- review fixes ------------------------------------------------------------------------


@R4
async def test_tenant_override_with_long_slug_fits_callback_limit() -> None:
    """``callback_data`` of an override button is its row id (env checks <= 64 bytes)."""
    staff, shop = _user(STAFF), _tenant("Shop A", "shop-a")
    env = Env(SUPER, [shop], users=[staff])
    env.overrides[staff.id] = [_row(ACCOUNTING_MANAGE, granted=True, tenant=shop.id)]
    await _overrides(env)
    await env.press("Управление бухгалтерией · Shop A")
    assert "accounting.manage" in env.text()
    await env.press("Удалить")
    await env.press("Да")
    assert ("remove_override", ACTOR, STAFF, ACCOUNTING_MANAGE, shop.id) in env.access.calls


@SCENARIO
async def test_save_keeps_inactive_and_unknown_permissions() -> None:
    role = make_role("Custom", "custom")
    env = Env(ROLES_ADMIN, roles=[role])
    env.role_perms[role.id] = frozenset({PRODUCTS_VIEW, LOGS_VIEW, "legacy.export"})
    env.live_slugs = SEED_PERMISSION_SLUGS - {LOGS_VIEW}
    await _open_card(env, "Custom \\(custom\\)")
    await env.press("Права роли")
    assert "Не редактируются здесь" in env.text()
    assert "legacy.export" in env.text() and "Просмотр логов (logs.view)" in env.text()
    await env.press("▫️ Создание товаров")
    await env.press(r".*Логи \d/1", regex=True)
    assert not any(b.startswith(("✅", "▫️")) for b in env.buttons())  # logs.view is inactive
    await env.press("Сохранить")
    assert env.roles.calls == [
        (
            "set_permissions",
            ACTOR,
            role.id,
            frozenset({PRODUCTS_VIEW, PRODUCTS_CREATE, LOGS_VIEW, "legacy.export"}),
        )
    ]


@R4
async def test_forged_checkbox_toggle_without_roles_manage_closes_dialog() -> None:
    role = make_role("Custom", "custom")
    env = Env(ROLES_ADMIN, roles=[role])
    await _open_card(env, "Custom \\(custom\\)")
    await env.press("Права роли")
    env.snapshot = VIEWER
    env.queries.calls.clear()
    await env.forge("r_perms", "r_perms", "r_perms")
    assert env.roles.calls == []
    assert not {"role", "role_permission_slugs"} & set(env.queries.calls)
    assert "Custom" not in env.text()


@R4
@pytest.mark.parametrize("kind", ["self", "superuser", "deleted"])
async def test_forged_staff_roles_of_protected_target_show_nothing(kind: str) -> None:
    target = _user(ACTOR if kind == "self" else STAFF, deleted=kind == "deleted")
    target.is_superuser = kind == "superuser"
    env = Env(SUPER, users=[target], roles=[make_role("Custom", "custom")])
    await env.start()
    await env.press("Сотрудники")
    await env.press(f".*\\({target.telegram_user_id}\\).*", regex=True)
    assert "Роли сотрудника" not in env.buttons()
    env.queries.calls.clear()
    await env.forge("u_card_back", "u_card_back", "u_roles")
    assert "Custom" not in env.text() and "user_role_ids" not in env.queries.calls
    await env.forge("u_card_back", "u_card_back", "u_ovr") if "u_card_back" in str(
        env.messages.last_message().reply_markup
    ) else None
    assert "user_overrides" not in env.queries.calls
    assert env.access.calls == []
