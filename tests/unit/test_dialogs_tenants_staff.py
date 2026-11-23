"""Master Bot dialogs of shops/staff/moderators via ``aiogram_dialog.test_tools`` (task 05).

Services and read queries are fakes resolved through a stub Dishka container put into the
middleware data under ``CONTAINER_NAME`` (what ``setup_dishka`` does in production).
Forged callbacks (hidden buttons, foreign item ids) are sent by rewriting the
``callback_data`` of a rendered button: aiogram-dialog checks ``when`` only at render time.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from app.modules.iam.admin_service import (
    validate_telegram_user_id,
    validate_tenant_settings,
)
from app.modules.iam.permissions import (
    MODERATORS_MANAGE,
    ROLES_MANAGE,
    SETTINGS_MANAGE,
    TENANTS_VIEW_ALL,
    USERS_MANAGE,
    USERS_VIEW,
)
from app.modules.iam.presentation.tg_master import staff as staff_presentation
from app.modules.iam.presentation.tg_master import tenants as tenants_presentation
from app.modules.iam.presentation.tg_master.staff import parse_telegram_id
from app.modules.iam.presentation.tg_master.tenants import parse_chat_id, parse_name, parse_slug
from app.modules.iam.snapshot import PermissionSnapshot
from app.shared_kernel.domain.exceptions import DomainError, ValidationError
from app.shared_kernel.presentation.tg_master.common import Outcome, error_text, short
from tests.unit._master_bot_env import (  # noqa: F401
    ACTOR,
    R4,
    SCENARIO,
    STAFF_READER,
    SUPER,
    VIEWER,
    Env,
    FakeContainer,
    FakeQueries,
    Forged,
    Recorder,
    _tenant,
    _user,
)

# --- dedicated BotClient timezone coverage ---------------------------------------------


def _set_timezone(staff: SimpleNamespace, timezone: str) -> SimpleNamespace:
    staff.timezone = timezone
    return staff


async def test_staff_timezone_onboarding_skip_persists_utc_and_reopens_menu() -> None:
    staff = _user(ACTOR)
    staff.timezone = None
    env = Env(SUPER, users=[staff])
    env.user_service.handlers["set_timezone"] = lambda actor, timezone: _set_timezone(
        staff, timezone
    )

    await env.start()
    assert "Настройте часовой пояс" in env.text()
    await env.press("Пропустить (оставить UTC)")
    assert staff.timezone == "UTC"
    assert ("set_timezone", ACTOR, "UTC") in env.user_service.calls
    assert "Магазины" in env.buttons()

    await env.start()
    assert "Магазины" in env.buttons()
    assert "Настройте часовой пояс" not in env.text()


async def test_staff_timezone_onboarding_preset_and_city_search_save_iana() -> None:
    staff = _user(ACTOR)
    staff.timezone = None
    env = Env(SUPER, users=[staff])
    env.user_service.handlers["set_timezone"] = lambda actor, timezone: _set_timezone(
        staff, timezone
    )

    await env.start()
    await env.press("Europe/Berlin")
    assert staff.timezone == "Europe/Berlin"
    assert "Магазины" in env.buttons()

    staff.timezone = None
    await env.start()
    await env.send("new york")
    assert "America/New_York" in env.buttons()
    await env.press("America/New_York")
    assert staff.timezone == "America/New_York"


async def test_forged_onboarding_result_is_rejected() -> None:
    staff = _user(ACTOR)
    staff.timezone = None
    env = Env(SUPER, users=[staff])
    env.user_service.handlers["set_timezone"] = lambda actor, timezone: _set_timezone(
        staff, timezone
    )

    await env.start()
    await env.send("new york")
    await env.forge("u_onboarding_zone", "America/New_York", "Europe/Berlin")
    assert staff.timezone is None
    assert not any(call[0] == "set_timezone" for call in env.user_service.calls)
    assert "Выберите часовой пояс" in env.text()


async def test_onboarding_skip_failed_save_stays_and_keeps_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SimpleNamespace(dialog_data={}, started=[])
    manager.middleware_data = {"event_from_user": SimpleNamespace(id=ACTOR)}
    manager.start = lambda *args, **kwargs: manager.started.append((args, kwargs))

    async def failed_mutate(*args: Any, **kwargs: Any) -> Outcome[None]:
        manager.dialog_data["notice"] = "failed"
        return _failed_outcome()

    monkeypatch.setattr(staff_presentation, "mutate", failed_mutate)
    await staff_presentation.on_onboarding_skip(
        cast(Any, SimpleNamespace()), cast(Any, SimpleNamespace()), manager
    )
    assert manager.started == []
    assert manager.dialog_data["notice"] == "failed"


async def test_onboarding_zone_failed_save_stays_and_keeps_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SimpleNamespace(dialog_data={"onboarding_zones": ["Europe/Berlin"]}, started=[])
    manager.middleware_data = {"event_from_user": SimpleNamespace(id=ACTOR)}
    manager.start = lambda *args, **kwargs: manager.started.append((args, kwargs))

    async def failed_mutate(*args: Any, **kwargs: Any) -> Outcome[None]:
        manager.dialog_data["notice"] = "failed"
        return _failed_outcome()

    monkeypatch.setattr(staff_presentation, "mutate", failed_mutate)
    await staff_presentation.on_onboarding_zone(
        cast(Any, SimpleNamespace(answer=lambda *args: None)),
        cast(Any, SimpleNamespace()),
        manager,
        "Europe/Berlin",
    )
    assert manager.started == []
    assert manager.dialog_data["notice"] == "failed"


def _failed_outcome() -> Outcome[None]:
    return Outcome(ok=False, code="permission_denied")


async def test_timezone_profile_failed_save_stays_and_keeps_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SimpleNamespace(
        dialog_data={}, middleware_data={"event_from_user": SimpleNamespace(id=ACTOR)}
    )
    manager.switched = []

    async def target_or_back(*args: Any, **kwargs: Any) -> int:
        return ACTOR

    async def failed_mutate(*args: Any, **kwargs: Any) -> Outcome[None]:
        manager.dialog_data["notice"] = "failed"
        return _failed_outcome()

    async def switch_to(*args: Any, **kwargs: Any) -> None:
        manager.switched.append((args, kwargs))

    manager.switch_to = switch_to
    monkeypatch.setattr(staff_presentation, "_target_or_back", target_or_back)
    monkeypatch.setattr(staff_presentation, "mutate", failed_mutate)
    await staff_presentation.on_timezone_value(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        manager,
        "Europe/Berlin",
    )
    assert manager.switched == []
    assert manager.dialog_data["notice"] == "failed"


async def test_timezone_getters_expose_localized_navigation_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SimpleNamespace(dialog_data={"timezone_query": "europe", "timezone_page": 0})

    async def current(_: Any) -> tuple[SimpleNamespace, SimpleNamespace]:
        return _tenant_current()

    monkeypatch.setattr(tenants_presentation, "_current", current)
    monkeypatch.setattr(tenants_presentation, "_can_manage", lambda _: True)
    presets = await tenants_presentation.timezone_presets_getter(manager)
    results = await tenants_presentation.timezone_results_getter(manager)
    assert presets["btn_search"] == "Поиск"
    assert "btn_next" in results


def _tenant_current() -> tuple[SimpleNamespace, SimpleNamespace]:
    return _tenant("Shop A", "shop-a"), SimpleNamespace()


async def test_tenant_timezone_preset_uses_manage_scope_and_persists_iana() -> None:
    shop = _tenant("Shop A", "shop-a")
    shop.timezone = "UTC"
    env = Env(SUPER, [shop])
    await env.start()
    await env.press("Магазины")
    await env.press("Shop A (shop-a)")
    await env.press("Настройки магазина")
    await env.press(".*Часовой пояс.*", regex=True)
    await env.press(".*Europe/Berlin.*", regex=True)
    assert shop.timezone == "Europe/Berlin"
    assert ("update", ACTOR, shop.id, shop.name, "Europe/Berlin") in env.tenants.calls


# --- section entry ----------------------------------------------------------------------


@R4
async def test_no_permission_no_section_button() -> None:
    env = Env(VIEWER)
    await env.start()
    assert "Магазины" not in env.buttons() and "Сотрудники" not in env.buttons()
    assert "Логи" in env.buttons()


@R4
async def test_forged_section_callback_is_rejected() -> None:
    env = Env(VIEWER, users=[_user(42)])
    await env.start()
    await env.forge("logs", "logs", "staff")
    await env.forge("logs", "logs", "shops")
    assert "users" not in env.queries.calls
    assert env.tenants.calls == []


# --- shops ------------------------------------------------------------------------------


@SCENARIO
async def test_create_tenant_calls_service_and_shows_card() -> None:
    env = Env(SUPER)
    await env.start()
    await env.press("Магазины")
    assert "Нет доступных магазинов" in env.text()
    await env.press("Создать магазин")
    await env.send("   ")
    assert "от 1 до 128 символов" in env.text()
    await env.send("Shop One")
    await env.send("Bad Slug!")  # rejected by TextInput, no service call
    assert "Некорректное значение" in env.text()
    await env.send("shop-one")
    assert ("create", ACTOR, "Shop One", "shop-one") in env.tenants.calls
    assert "Готово." in env.text()
    assert "Магазин: Shop One" in env.text() and "shop-one" in env.text()


@R4
async def test_service_error_is_localized() -> None:
    env = Env(SUPER)
    env.tenants.errors["create"] = DomainError("taken", code="tenant_slug_taken")
    await env.start()
    await env.press("Магазины")
    await env.press("Создать магазин")
    await env.send("Shop")
    await env.send("shop")
    assert error_text("tenant_slug_taken") in env.text()


@SCENARIO
async def test_deactivate_with_confirmation_and_stale_cache_warning() -> None:
    shop = _tenant("Shop A", "shop-a")
    env = Env(SUPER, [shop])
    env.tenants.cache_invalidated = False
    await env.start()
    await env.press("Магазины")
    await env.press("Shop A (shop-a)")
    await env.press("Деактивировать")
    assert "Деактивировать магазин?" in env.text()  # confirmation window
    await env.press("Да")
    assert env.tenants.calls[-1] == ("deactivate", ACTOR, shop.id)
    assert "кэш прав обновится с задержкой" in env.text()


@SCENARIO
async def test_rename_and_settings_flag_language_chats() -> None:
    shop = _tenant("Shop A", "shop-a")
    env = Env(SUPER, [shop])
    await env.start()
    await env.press("Магазины")
    await env.press("Shop A (shop-a)")
    await env.press("Переименовать")
    await env.send("Shop B")
    assert ("update", ACTOR, shop.id, "Shop B") in env.tenants.calls
    assert "Магазин: Shop B" in env.text()
    await env.press("Настройки магазина")
    await env.press(".*Магазин открыт", regex=True)
    assert ("update_settings", ACTOR, shop.id, {"is_store_open": True}) in env.tenants.calls
    await env.press("Язык по умолчанию: глобальный")
    assert "✅ Глобальный по умолчанию" in env.buttons()  # current language marked
    await env.press(".*English \\(en\\)", regex=True)
    await env.press("Язык по умолчанию: en")
    await env.press(".*Глобальный по умолчанию", regex=True)
    assert env.settings[shop.id].default_language_code is None
    await env.press("Чат поддержки: —")
    assert "-1001234567890" in env.text()
    await env.send("99999999999999999999")  # bigint overflow -> input error, no call
    assert "Некорректное значение" in env.text()
    await env.send("-1001234567890")
    assert env.settings[shop.id].support_chat_id == -1001234567890
    await env.press("Чат поддержки: -1001234567890")
    await env.send("-")
    assert env.settings[shop.id].support_chat_id is None


@SCENARIO
async def test_restore_from_deleted() -> None:
    gone = _tenant("Shop D", "shop-d", deleted=True)
    env = Env(SUPER, [gone])
    await env.start()
    await env.press("Магазины")
    await env.press("Удалённые магазины")
    await env.press("Shop D (shop-d)")
    await env.press("Восстановить")
    assert ("restore", ACTOR, gone.id) in env.tenants.calls
    assert "Статус: активен" in env.text()


@R4
async def test_tenant_list_is_scoped_by_service() -> None:
    visible = [_tenant(f"Shop {i}", f"shop-{i}") for i in range(12)]
    env = Env(
        PermissionSnapshot(user_id=uuid4(), tenants=frozenset(t.id for t in visible)), visible
    )
    await env.start()
    await env.press("Магазины")
    assert ("list_tenants", ACTOR) in env.tenants.calls
    buttons = env.buttons()
    assert "Shop 0 (shop-0)" in buttons and "Shop 11 (shop-11)" not in buttons  # page of 10
    assert "Создать магазин" not in buttons  # no tenants.manage


@R4
async def test_forged_foreign_tenant_and_hidden_deleted_are_rejected() -> None:
    own = _tenant("Own", "own")
    foreign = _tenant("Foreign", "foreign")
    gone = _tenant("Gone", "gone", deleted=True)
    snapshot = PermissionSnapshot(user_id=uuid4(), tenants=frozenset({own.id}))
    env = Env(snapshot, [own, foreign, gone], visible=[own])
    await env.start()
    await env.press("Магазины")
    await env.forge("t_sel", str(own.id), str(foreign.id))
    assert "Магазин не найден" in env.text() and "Foreign" not in env.text()
    await env.forge("t_sel", f"t_sel:{own.id}", "t_deleted")
    assert "Нет доступа" in env.text() and "Gone" not in env.text()
    assert "deleted_tenants" not in env.queries.calls


# --- staff ------------------------------------------------------------------------------


@SCENARIO
async def test_add_staff_by_telegram_id() -> None:
    env = Env(SUPER)
    await env.start()
    await env.press("Сотрудники")
    await env.press("Добавить сотрудника")
    await env.send("abc")
    await env.send("99999999999999999999")  # bigint overflow
    assert env.user_service.calls == []
    await env.send("777000111")
    assert env.user_service.calls == [("add_by_telegram_id", ACTOR, 777000111)]
    assert "777000111" in env.text() and "• Модератор" in env.text()


@SCENARIO
async def test_add_deleted_staff_opens_card_with_restore() -> None:
    env = Env(SUPER, users=[_user(888, deleted=True)])
    env.user_service.errors["add_by_telegram_id"] = DomainError("x", code="user_deleted")
    await env.start()
    await env.press("Сотрудники")
    assert "— (888) 🗑" in env.buttons()
    await env.press("Добавить сотрудника")
    await env.send("888")
    assert error_text("user_deleted") in env.text()
    assert "Восстановить" in env.buttons()
    await env.press("Восстановить")
    assert ("restore", ACTOR, 888) in env.user_service.calls


@SCENARIO
async def test_own_profile_has_no_status_buttons() -> None:
    env = Env(SUPER, users=[_user(ACTOR)])
    await env.start()
    await env.press("Сотрудники")
    await env.press(f"— ({ACTOR})")
    buttons = env.buttons()
    for hidden in ("Деактивировать", "Удалить", "Магазины сотрудника"):
        assert hidden not in buttons


@SCENARIO
async def test_assign_and_unassign_moderator() -> None:
    shop = _tenant("Shop A", "shop-a")
    staff = _user(321)
    env = Env(SUPER, [shop], users=[staff])
    await env.start()
    await env.press("Сотрудники")
    await env.press("— (321)")
    await env.press("Магазины сотрудника")
    env.access.handlers["assign_moderator"] = lambda *a: env.moderated.update({staff.id: [shop]})
    await env.press("▫️ Shop A (shop-a)")
    assert ("assign_moderator", ACTOR, 321, shop.id) in env.access.calls
    assert "Готово." in env.text()
    await env.press("✅ Shop A (shop-a)")
    assert ("unassign_moderator", ACTOR, 321, shop.id) in env.access.calls


@R4
async def test_staff_card_shops_limited_to_viewer_scope() -> None:
    own, foreign = _tenant("Own", "own"), _tenant("Foreign", "foreign")
    staff = _user(321)
    snapshot = PermissionSnapshot(
        user_id=uuid4(),
        role_permissions={
            USERS_VIEW: frozenset({"senior"}),
            MODERATORS_MANAGE: frozenset({"senior"}),
        },
        tenants=frozenset({own.id}),
    )
    env = Env(snapshot, [own, foreign], visible=[own], users=[staff])
    env.moderated[staff.id] = [own, foreign]
    await env.start()
    await env.press("Сотрудники")
    await env.press("— (321)")
    assert "Own (own)" in env.text() and "Foreign" not in env.text()


@R4
async def test_forged_staff_card_without_right_is_rejected() -> None:
    """A staff dialog left open after the right was revoked: no user data is read."""
    staff = _user(321)
    env = Env(SUPER, users=[staff])
    await env.start()
    await env.press("Сотрудники")
    env.snapshot = VIEWER  # the right is revoked while the dialog is open
    env.queries.calls.clear()
    await env.forge("u_sel", "u_sel:321", "u_sel:321")
    assert "user" not in env.queries.calls and "users" not in env.queries.calls
    assert "(321)" not in env.text()


# --- pure parsers / validators -------------------------------------------------------


def test_unknown_error_code_is_generic() -> None:
    assert error_text("something_new") == "Не удалось выполнить действие."
    assert error_text("invalid_whatever") == "Некорректные данные."
    assert error_text("invalid_name") != error_text("invalid_tenant_slug")


def test_input_parsers() -> None:
    assert parse_slug(" shop-1 ") == "shop-1"
    for bad in ("Shop", "a--b", "-a", "a" * 65):
        with pytest.raises(ValueError):
            parse_slug(bad)
    assert parse_name("  Shop  ") == "Shop"
    for bad_name in ("   ", "x" * 129):
        with pytest.raises(ValueError):
            parse_name(bad_name)
    assert parse_chat_id("-") is None and parse_chat_id("-100123") == -100123
    for bad_chat in ("0", "9" * 20, "1_000", "abc", str(2**63)):
        with pytest.raises(ValueError):
            parse_chat_id(bad_chat)
    assert parse_telegram_id("42") == 42
    for bad_id in ("0", "-5", str(2**63), "1_0"):
        with pytest.raises(ValueError):
            parse_telegram_id(bad_id)
    assert short("x" * 70).endswith("…") and len(short("x" * 70)) == 60


def test_service_validators_bigint_range() -> None:
    with pytest.raises(ValidationError):
        validate_telegram_user_id(2**63)
    for value in (0, 2**63, -(2**63)):
        with pytest.raises(ValidationError):
            validate_tenant_settings({"support_chat_id": value})
    assert validate_tenant_settings({"news_chat_id": -1001234567890}) == {
        "news_chat_id": -1001234567890
    }


# --- re-review F1/F3 -------------------------------------------------------------------


@R4
@pytest.mark.parametrize("superuser", [False, True], ids=["view_all", "superuser"])
async def test_staff_card_hides_deleted_shops_without_tenants_manage(superuser: bool) -> None:
    """Tenant soft-delete keeps ``tenant_moderators``: the deleted shop needs tenants.manage."""
    live, gone = _tenant("Live", "live"), _tenant("Gone", "gone", deleted=True)
    staff = _user(321)
    snapshot = (
        SUPER
        if superuser
        else PermissionSnapshot(
            user_id=uuid4(),
            role_permissions={
                USERS_VIEW: frozenset({"senior"}),
                MODERATORS_MANAGE: frozenset({"senior"}),
                TENANTS_VIEW_ALL: frozenset({"senior"}),
            },
        )
    )
    env = Env(snapshot, [live, gone], users=[staff])
    env.moderated[staff.id] = [gone, live]
    await env.start()
    await env.press("Сотрудники")
    await env.press("— (321)")
    assert "Live (live)" in env.text()
    if superuser:
        assert "Gone (gone) 🗑" in env.text()
    else:
        assert "Gone" not in env.text() and "gone" not in env.text()


@R4
async def test_forged_settings_and_language_of_foreign_tenant_show_nothing() -> None:
    shop = _tenant("Shop A", "shop-a")
    own = PermissionSnapshot(user_id=uuid4(), tenants=frozenset({shop.id}))
    env = Env(own, [shop])
    await env.start()
    await env.press("Магазины")
    await env.press("Shop A (shop-a)")
    # The shop leaves the actor's scope while its card is still open.
    env.snapshot = PermissionSnapshot(user_id=uuid4(), tenants=frozenset({uuid4()}))
    await env.forge("t_card_back", "t_card_back", "t_settings")
    assert "Магазин не найден" in env.text() and "Shop A" not in env.text()
    assert not any("Магазин открыт" in b for b in env.buttons())
    await env.forge("t_settings_back", "t_settings_back", "t_lang")
    assert "Магазин не найден" in env.text() and "English" not in str(env.buttons())
    assert not any(call[0] == "update_settings" for call in env.tenants.calls)


@R4
async def test_forged_staff_shops_without_moderators_manage_are_rejected() -> None:
    shop = _tenant("Shop A", "shop-a")
    staff = _user(321)
    roles_admin = PermissionSnapshot(
        user_id=uuid4(),
        role_permissions={USERS_VIEW: frozenset({"a"}), USERS_MANAGE: frozenset({"a"})},
    )
    env = Env(roles_admin, [shop], users=[staff])
    await env.start()
    await env.press("Сотрудники")
    await env.press("— (321)")
    assert "Магазины сотрудника" not in env.buttons()
    await env.forge("u_card_back", "u_card_back", "u_tenants")
    assert "Нет доступа" in env.text() and "Shop A" not in str(env.buttons())
    await env.forge("u_t_back", "u_t_back", f"u_t_sel:{shop.id}")
    assert env.access.calls == []


@R4
async def test_staff_reader_sees_cards_without_manage_buttons() -> None:
    """users.view only: list and card are readable, add/status/shop buttons are hidden."""
    staff = _user(321)
    env = Env(STAFF_READER, users=[staff])
    await env.start()
    assert "Сотрудники" in env.buttons() and "Логи" not in env.buttons()
    await env.press("Сотрудники")
    assert "Добавить сотрудника" not in env.buttons()
    await env.press("— (321)")
    assert "(321)" in env.text()
    assert not ({"Деактивировать", "Удалить", "Магазины сотрудника"} & set(env.buttons()))


@R4
async def test_roles_manage_alone_does_not_open_staff() -> None:
    """roles.manage / moderators.manage no longer open the staff section (users.view does)."""
    snapshot = PermissionSnapshot(
        user_id=uuid4(),
        role_permissions={
            ROLES_MANAGE: frozenset({"r"}),
            MODERATORS_MANAGE: frozenset({"r"}),
        },
    )
    env = Env(snapshot, users=[_user(42)])
    await env.start()
    assert "Сотрудники" not in env.buttons()
    await env.forge("roles", "roles", "staff")
    assert "users" not in env.queries.calls


@R4
async def test_settings_manage_alone_does_not_manage_tenants() -> None:
    """settings.manage is system parameters only: no create, deleted shops are hidden."""
    own, gone = _tenant("Own", "own"), _tenant("Gone", "gone", deleted=True)
    snapshot = PermissionSnapshot(
        user_id=uuid4(),
        role_permissions={
            SETTINGS_MANAGE: frozenset({"r"}),
            TENANTS_VIEW_ALL: frozenset({"r"}),
        },
    )
    env = Env(snapshot, [own, gone], visible=[own])
    await env.start()
    await env.press("Магазины")
    assert "Создать магазин" not in env.buttons()
    await env.forge("t_sel", f"t_sel:{own.id}", "t_deleted")
    assert "Gone" not in env.text()
    assert "deleted_tenants" not in env.queries.calls


@R4
@pytest.mark.parametrize("kind", ["moderator", "senior", "superuser"])
async def test_staff_shops_button_hidden_for_global_access(kind: str) -> None:
    """Global tenants.view_all / superuser is never assigned to shops: no button."""
    staff = _user(321)
    if kind == "superuser":
        staff.is_superuser = True
    env = Env(SUPER, [_tenant("Shop A", "shop-a")], users=[staff])
    if kind == "senior":
        env.global_access.add(321)
    await env.start()
    await env.press("Сотрудники")
    await env.press("— (321)")
    assert ("Магазины сотрудника" in env.buttons()) is (kind == "moderator")


@R4
async def test_forged_staff_shops_of_global_access_user_show_nothing() -> None:
    """Forged callback to the assignment window of a senior: no shop list, no service call."""
    shop = _tenant("Shop A", "shop-a")
    staff = _user(321)
    env = Env(SUPER, [shop], users=[staff])
    env.global_access.add(321)
    await env.start()
    await env.press("Сотрудники")
    await env.press("— (321)")
    assert "Магазины сотрудника" not in env.buttons()
    await env.forge("deactivate", "deactivate", "u_tenants")
    assert "доступом ко всем магазинам" in env.text()
    assert "Shop A" not in env.text()
    assert ("list_tenants", ACTOR) not in env.tenants.calls
    assert env.access.calls == []
