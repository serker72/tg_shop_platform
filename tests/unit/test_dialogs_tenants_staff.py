"""Master Bot dialogs of shops/staff/moderators via ``aiogram_dialog.test_tools`` (task 05).

Services and read queries are fakes resolved through a stub Dishka container put into the
middleware data under ``CONTAINER_NAME`` (what ``setup_dishka`` does in production).
Forged callbacks (hidden buttons, foreign item ids) are sent by rewriting the
``callback_data`` of a rendered button: aiogram-dialog checks ``when`` only at render time.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, Message
from aiogram_dialog import setup_dialogs
from aiogram_dialog.test_tools import BotClient, MockMessageManager
from aiogram_dialog.test_tools.keyboard import InlineButtonTextLocator

from app.entrypoints.master_bot.menu import build_menu_router
from app.entrypoints.master_bot.middlewares import SNAPSHOT_KEY
from app.modules.iam.admin_service import (
    TENANT_SETTINGS_FIELDS,
    AccessAdminService,
    TenantAdminService,
    UserAdminService,
    validate_telegram_user_id,
    validate_tenant_settings,
)
from app.modules.iam.permissions import (
    LOGS_VIEW,
    MODERATORS_MANAGE,
    ROLES_MANAGE,
    SETTINGS_MANAGE,
    TENANTS_VIEW_ALL,
    USERS_MANAGE,
    USERS_VIEW,
)
from app.modules.iam.presentation.tg_master.common import CONTAINER_NAME, error_text, short
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.iam.presentation.tg_master.staff import parse_telegram_id
from app.modules.iam.presentation.tg_master.tenants import parse_chat_id, parse_name, parse_slug
from app.modules.iam.snapshot import PermissionSnapshot
from app.shared_kernel.domain.exceptions import DomainError, ValidationError

R4 = pytest.mark.req("R-II.4")
SCENARIO = pytest.mark.req("R-II.4", "S-II.3")

ACTOR = 501
SUPER = PermissionSnapshot(user_id=uuid4(), is_superuser=True)
#: Holds only logs.view: no shops, no staff.
VIEWER = PermissionSnapshot(user_id=uuid4(), role_permissions={LOGS_VIEW: frozenset({"r"})})
#: Reads staff (users.view) but can not manage them.
STAFF_READER = PermissionSnapshot(
    user_id=uuid4(), role_permissions={USERS_VIEW: frozenset({"senior"})}
)


def _tenant(name: str, slug: str, *, deleted: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), name=name, slug=slug, is_active=True, is_deleted=deleted)


def _user(telegram_user_id: int, *, deleted: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        telegram_user_id=telegram_user_id,
        telegram_user_name=None,
        is_active=True,
        is_deleted=deleted,
        is_superuser=False,
    )


class Recorder:
    """Fake admin service: records calls, optional per-method handler or error."""

    def __init__(self, **handlers: Callable[..., Any]) -> None:
        self.handlers = handlers
        self.errors: dict[str, DomainError] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.cache_invalidated = True
        self._noop = False

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, *args, *kwargs.values()))
            if name in self.errors:
                raise self.errors[name]
            handler = self.handlers.get(name)
            return handler(*args, **kwargs) if handler else None

        return call


class FakeQueries:
    def __init__(self, env: Env) -> None:
        self.env = env
        self.calls: list[str] = []

    async def tenant(self, tenant_id: UUID) -> Any:
        self.calls.append("tenant")
        tenant = self.env.store.get(tenant_id)
        return None if tenant is None else (tenant, self.env.settings[tenant_id])

    async def deleted_tenants(self) -> list[Any]:
        self.calls.append("deleted_tenants")
        return [t for t in self.env.store.values() if t.is_deleted]

    async def live_languages(self) -> list[Any]:
        return [SimpleNamespace(code="en", name="English")]

    async def users(self) -> list[Any]:
        self.calls.append("users")
        return list(self.env.users.values())

    async def user(self, telegram_user_id: int) -> Any:
        self.calls.append("user")
        return self.env.users.get(telegram_user_id)

    async def user_roles(self, user_id: UUID) -> list[str]:
        return ["Модератор"]

    async def has_global_access(self, user: Any) -> bool:
        return bool(user.is_superuser) or user.telegram_user_id in self.env.global_access

    async def user_tenants(self, user_id: UUID) -> list[Any]:
        return list(self.env.moderated.get(user_id, []))


class FakeContainer:
    def __init__(self, deps: dict[type, Any]) -> None:
        self.deps = deps

    async def get(self, kind: type) -> Any:
        return self.deps[kind]


class Forged:
    """Locator returning a button with arbitrary ``callback_data``."""

    def __init__(self, data: str) -> None:
        self.data = data

    def find_button(self, message: Message) -> InlineKeyboardButton:
        return InlineKeyboardButton(text="forged", callback_data=self.data)


class Env:
    def __init__(
        self,
        snapshot: PermissionSnapshot,
        tenants: list[SimpleNamespace] | None = None,
        *,
        visible: list[SimpleNamespace] | None = None,
        users: list[SimpleNamespace] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.store: dict[UUID, SimpleNamespace] = {t.id: t for t in tenants or []}
        self.settings: dict[UUID, SimpleNamespace] = {t.id: self._settings() for t in tenants or []}
        self.users: dict[int, SimpleNamespace] = {u.telegram_user_id: u for u in users or []}
        self.moderated: dict[UUID, list[SimpleNamespace]] = {}
        live = [t for t in self.store.values() if not t.is_deleted]
        self.tenants = Recorder(
            list_tenants=lambda actor: visible if visible is not None else live,
            create=self._create,
            update=self._rename,
            restore=lambda actor, tenant_id: self._set(tenant_id, is_deleted=False),
            deactivate=lambda actor, tenant_id: self._set(tenant_id, is_active=False),
            update_settings=self._update_settings,
        )
        self.user_service = Recorder(add_by_telegram_id=self._add_user)
        self.access = Recorder()
        self.queries = FakeQueries(self)
        self.global_access: set[int] = set()
        container = FakeContainer(
            {
                TenantAdminService: self.tenants,
                UserAdminService: self.user_service,
                AccessAdminService: self.access,
                IamAdminQueries: self.queries,
            }
        )

        async def inject(
            handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
            event: Any,
            data: dict[str, Any],
        ) -> Any:
            data[SNAPSHOT_KEY] = self.snapshot
            data[CONTAINER_NAME] = container
            return await handler(event, data)

        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.update.outer_middleware(inject)
        dispatcher.include_router(build_menu_router())
        self.messages = MockMessageManager()  # type: ignore[no-untyped-call]
        setup_dialogs(dispatcher, message_manager=self.messages)
        self.client = BotClient(dispatcher, user_id=ACTOR, chat_id=ACTOR)

    # --- fake behaviour ----------------------------------------------------------------

    @staticmethod
    def _settings() -> SimpleNamespace:
        return SimpleNamespace(
            **{k: (False if kind == "bool" else None) for k, kind in TENANT_SETTINGS_FIELDS.items()}
        )

    def _create(self, actor: int, *, name: str, slug: str) -> SimpleNamespace:
        tenant = _tenant(name, slug)
        self.store[tenant.id] = tenant
        self.settings[tenant.id] = self._settings()
        return tenant

    def _rename(self, actor: int, tenant_id: UUID, *, name: str) -> SimpleNamespace:
        return self._set(tenant_id, name=name)

    def _set(self, tenant_id: UUID, **values: Any) -> SimpleNamespace:
        tenant = self.store[tenant_id]
        for key, value in values.items():
            setattr(tenant, key, value)
        return tenant

    def _update_settings(self, actor: int, tenant_id: UUID, changes: dict[str, Any]) -> Any:
        for key, value in changes.items():
            setattr(self.settings[tenant_id], key, value)
        return self.settings[tenant_id]

    def _add_user(self, actor: int, telegram_user_id: int) -> SimpleNamespace:
        self.users[telegram_user_id] = _user(telegram_user_id)
        return self.users[telegram_user_id]

    # --- driving ------------------------------------------------------------------------

    def text(self) -> str:
        return str(self.messages.last_message().text)

    def buttons(self) -> list[str]:
        markup = self.messages.last_message().reply_markup
        return [b.text for row in markup.inline_keyboard for b in row] if markup else []

    def _data(self, needle: str) -> str:
        markup = self.messages.last_message().reply_markup
        assert markup is not None
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data and needle in button.callback_data:
                    return button.callback_data
        raise AssertionError(f"no button with {needle!r}")

    async def start(self) -> None:
        await self.client.send("/start")

    async def press(self, text: str, *, regex: bool = False) -> None:
        last = self.messages.last_message()
        self.messages.reset_history()  # type: ignore[no-untyped-call]
        pattern = text if regex else re.escape(text)
        await self.client.click(last, InlineButtonTextLocator(pattern))

    async def forge(self, needle: str, replace: str, by: str) -> None:
        """Click a button whose ``callback_data`` (found by ``needle``) is rewritten."""
        last = self.messages.last_message()
        data = self._data(needle).replace(replace, by)
        await self.client.click(last, Forged(data))

    async def send(self, text: str) -> None:
        self.messages.reset_history()  # type: ignore[no-untyped-call]
        await self.client.send(text)


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
