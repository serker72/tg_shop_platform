"""Master Bot dialogs of shops/staff/moderators via ``aiogram_dialog.test_tools`` (task 05).

Services and read queries are fakes resolved through a stub Dishka container put into the
middleware data under ``CONTAINER_NAME`` (what ``setup_dishka`` does in production).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
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
)
from app.modules.iam.permissions import USERS_VIEW
from app.modules.iam.presentation.tg_master.common import CONTAINER_NAME, error_text
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.iam.presentation.tg_master.staff import parse_telegram_id
from app.modules.iam.presentation.tg_master.tenants import parse_chat_id, parse_slug
from app.modules.iam.snapshot import PermissionSnapshot
from app.shared_kernel.domain.exceptions import DomainError

pytestmark = pytest.mark.req("R-II.4", "S-II.3")

ACTOR = 501
SUPER = PermissionSnapshot(user_id=uuid4(), is_superuser=True)
VIEWER = PermissionSnapshot(user_id=uuid4(), role_permissions={USERS_VIEW: frozenset({"r"})})


def _tenant(name: str, slug: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), name=name, slug=slug, is_active=True, is_deleted=False)


def _settings() -> SimpleNamespace:
    values = {k: (False if kind == "bool" else None) for k, kind in TENANT_SETTINGS_FIELDS.items()}
    return SimpleNamespace(**values)


def _user(telegram_user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        telegram_user_id=telegram_user_id,
        telegram_user_name=None,
        is_active=True,
        is_deleted=False,
        is_superuser=False,
    )


class FakeTenants:
    def __init__(self, store: dict[UUID, SimpleNamespace], visible: list[SimpleNamespace]) -> None:
        self.store, self.visible = store, visible
        self.cache_invalidated = True
        self.calls: list[tuple[Any, ...]] = []
        self.error: DomainError | None = None

    async def list_tenants(self, actor: int) -> list[SimpleNamespace]:
        self.calls.append(("list", actor))
        return self.visible

    async def create(self, actor: int, *, name: str, slug: str) -> SimpleNamespace:
        self.calls.append(("create", actor, name, slug))
        if self.error is not None:
            raise self.error
        tenant = _tenant(name, slug)
        self.store[tenant.id] = tenant
        return tenant

    async def deactivate(self, actor: int, tenant_id: UUID) -> SimpleNamespace:
        self.calls.append(("deactivate", actor, tenant_id))
        self.cache_invalidated = False
        self.store[tenant_id].is_active = False
        return self.store[tenant_id]


class FakeUsers:
    def __init__(self, users: dict[int, SimpleNamespace]) -> None:
        self.users = users
        self.cache_invalidated = True
        self.calls: list[tuple[Any, ...]] = []

    async def add_by_telegram_id(self, actor: int, telegram_user_id: int) -> SimpleNamespace:
        self.calls.append(("add", actor, telegram_user_id))
        self.users[telegram_user_id] = _user(telegram_user_id)
        return self.users[telegram_user_id]


class FakeQueries:
    def __init__(self, tenants: dict[UUID, SimpleNamespace], users: dict[int, Any]) -> None:
        self.tenants, self.users_by_id = tenants, users

    async def tenant(self, tenant_id: UUID) -> Any:
        tenant = self.tenants.get(tenant_id)
        return None if tenant is None else (tenant, _settings())

    async def deleted_tenants(self) -> list[Any]:
        return []

    async def live_languages(self) -> list[Any]:
        return []

    async def users(self) -> list[Any]:
        return list(self.users_by_id.values())

    async def user(self, telegram_user_id: int) -> Any:
        return self.users_by_id.get(telegram_user_id)

    async def user_roles(self, user_id: UUID) -> list[Any]:
        return ["Модератор"]

    async def user_tenants(self, user_id: UUID) -> list[Any]:
        return []


class FakeContainer:
    def __init__(self, deps: dict[type, Any]) -> None:
        self.deps = deps

    async def get(self, kind: type) -> Any:
        return self.deps[kind]


class Env:
    def __init__(self, snapshot: PermissionSnapshot, visible: list[SimpleNamespace] | None = None):
        self.store: dict[UUID, SimpleNamespace] = {t.id: t for t in visible or []}
        self.users: dict[int, SimpleNamespace] = {}
        self.tenants = FakeTenants(self.store, visible or [])
        self.user_service = FakeUsers(self.users)
        container = FakeContainer(
            {
                TenantAdminService: self.tenants,
                UserAdminService: self.user_service,
                AccessAdminService: SimpleNamespace(cache_invalidated=True),
                IamAdminQueries: FakeQueries(self.store, self.users),
            }
        )

        async def inject(
            handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
            event: Any,
            data: dict[str, Any],
        ) -> Any:
            data[SNAPSHOT_KEY] = snapshot
            data[CONTAINER_NAME] = container
            return await handler(event, data)

        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.update.outer_middleware(inject)
        dispatcher.include_router(build_menu_router())
        self.messages = MockMessageManager()  # type: ignore[no-untyped-call]
        setup_dialogs(dispatcher, message_manager=self.messages)
        self.client = BotClient(dispatcher, user_id=ACTOR, chat_id=ACTOR)

    def text(self) -> str:
        return str(self.messages.last_message().text)

    def buttons(self) -> list[str]:
        markup = self.messages.last_message().reply_markup
        return [b.text for row in markup.inline_keyboard for b in row] if markup else []

    async def start(self) -> None:
        await self.client.send("/start")

    async def press(self, text: str) -> None:
        last = self.messages.last_message()
        self.messages.reset_history()  # type: ignore[no-untyped-call]
        await self.client.click(last, InlineButtonTextLocator(text))

    async def send(self, text: str) -> None:
        self.messages.reset_history()  # type: ignore[no-untyped-call]
        await self.client.send(text)


async def test_no_permission_no_section_button() -> None:
    env = Env(VIEWER)
    await env.start()
    assert "Магазины" not in env.buttons()
    assert "Сотрудники" in env.buttons()


async def test_create_tenant_calls_service_and_shows_card() -> None:
    env = Env(SUPER)
    await env.start()
    await env.press("Магазины")
    await env.press("Создать магазин")
    await env.send("Shop One")
    await env.send("Bad Slug!")  # rejected by TextInput, no service call
    assert "Некорректное значение" in env.text()
    await env.send("shop-one")
    assert ("create", ACTOR, "Shop One", "shop-one") in env.tenants.calls
    assert "Магазин: Shop One" in env.text() and "shop-one" in env.text()


async def test_service_error_is_localized() -> None:
    env = Env(SUPER)
    env.tenants.error = DomainError("taken", code="tenant_slug_taken")
    await env.start()
    await env.press("Магазины")
    await env.press("Создать магазин")
    await env.send("Shop")
    await env.send("shop")
    assert error_text("tenant_slug_taken") in env.text()


async def test_cache_not_invalidated_shows_warning_after_confirm() -> None:
    shop = _tenant("Shop A", "shop-a")
    env = Env(SUPER, [shop])
    await env.start()
    await env.press("Магазины")
    await env.press("Shop A \\(shop-a\\)")
    await env.press("Деактивировать")
    assert "Деактивировать магазин?" in env.text()  # confirmation window
    await env.press("Да")
    assert env.tenants.calls[-1] == ("deactivate", ACTOR, shop.id)
    assert "кэш прав обновится с задержкой" in env.text()


async def test_tenant_list_is_scoped_by_service() -> None:
    visible = [_tenant(f"Shop {i}", f"shop-{i}") for i in range(12)]
    env = Env(PermissionSnapshot(user_id=uuid4(), tenants=frozenset({uuid4()})), visible)
    await env.start()
    await env.press("Магазины")
    assert ("list", ACTOR) in env.tenants.calls
    buttons = env.buttons()
    assert "Shop 0 (shop-0)" in buttons and "Shop 11 (shop-11)" not in buttons  # page of 10
    assert "Создать магазин" not in buttons  # no settings.manage


async def test_add_staff_by_telegram_id() -> None:
    env = Env(SUPER)
    await env.start()
    await env.press("Сотрудники")
    await env.press("Добавить сотрудника")
    await env.send("abc")
    assert env.user_service.calls == []
    await env.send("777000111")
    assert env.user_service.calls == [("add", ACTOR, 777000111)]
    assert "777000111" in env.text() and "• Модератор" in env.text()


def test_unknown_error_code_is_generic() -> None:
    assert error_text("something_new") == "Не удалось выполнить действие."
    assert error_text("invalid_name") == "Некорректные данные."


def test_input_parsers() -> None:
    assert parse_slug(" shop-1 ") == "shop-1"
    for bad in ("Shop", "a--b", "-a", "a" * 65):
        with pytest.raises(ValueError):
            parse_slug(bad)
    assert parse_chat_id("-") is None and parse_chat_id("-100123") == -100123
    assert parse_telegram_id("42") == 42
    with pytest.raises(ValueError):
        parse_telegram_id("0")
