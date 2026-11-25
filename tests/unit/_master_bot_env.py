"""Shared test environment of the Master Bot dialogs (``aiogram_dialog.test_tools``).

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
    RoleAdminService,
    TenantAdminService,
    UserAdminService,
)
from app.modules.iam.permissions import (
    LOGS_VIEW,
    SEED_PERMISSION_SLUGS,
    USERS_VIEW,
)
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.iam.service import PermissionService
from app.modules.iam.snapshot import PermissionSnapshot
from app.modules.logging.export import ExportService
from app.modules.logging.filter_options import ActivityFilterOptions
from app.modules.logging.query import ActivityQueryRepository
from app.modules.system.service import SystemSettingsService
from app.shared_kernel.domain.exceptions import DomainError
from app.shared_kernel.presentation.tg_master.common import CONTAINER_NAME

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


def make_role(
    name: str, slug: str, *, system: bool = False, active: bool = True
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), name=name, slug=slug, is_system=system, is_active=active, is_deleted=False
    )


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
        #: Explicit ``stale_caches``; ``None`` -> like the IAM admin services.
        self.stale: tuple[str, ...] | None = None

    @property
    def stale_caches(self) -> tuple[str, ...]:
        if self.stale is not None:
            return self.stale
        return () if self.cache_invalidated else ("permissions",)

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

    async def roles(self) -> list[Any]:
        self.calls.append("roles")
        return [r for r in self.env.role_store.values() if not r.is_deleted]

    async def role(self, role_id: UUID) -> Any:
        self.calls.append("role")
        role = self.env.role_store.get(role_id)
        return None if role is None or role.is_deleted else role

    async def user_role_ids(self, user_id: UUID) -> frozenset[UUID]:
        self.calls.append("user_role_ids")
        return frozenset(self.env.user_roles.get(user_id, set()))

    async def user_overrides(self, user_id: UUID) -> list[Any]:
        self.calls.append("user_overrides")
        return list(self.env.overrides.get(user_id, []))

    async def live_permission_slugs(self) -> frozenset[str]:
        return self.env.live_slugs

    async def user_brief(self, user_id: UUID) -> tuple[int, str | None] | None:
        self.calls.append("user_brief")
        for user in self.env.users.values():
            if user.id == user_id:
                return user.telegram_user_id, user.telegram_user_name
        return None

    async def role_permission_slugs(self, role_id: UUID) -> frozenset[str]:
        self.calls.append("role_permission_slugs")
        return self.env.role_perms.get(role_id, frozenset())


#: Telegram limit of ``InlineKeyboardButton.callback_data`` (bytes).
CALLBACK_DATA_LIMIT = 64


class CheckedMessageManager(MockMessageManager):
    """Fails a test as soon as a rendered button carries ``callback_data`` longer than the
    Telegram limit (Telegram rejects such a keyboard with ``BUTTON_DATA_INVALID``)."""

    async def show_message(self, bot: Any, new_message: Any, old_message: Any) -> Any:
        markup = new_message.reply_markup
        for row in getattr(markup, "inline_keyboard", None) or []:
            for button in row:
                data = button.callback_data or ""
                assert len(data.encode()) <= CALLBACK_DATA_LIMIT, (
                    f"callback_data too long: {data!r}"
                )
        return await super().show_message(bot, new_message, old_message)


class FakePermissionService:
    """``effective_permissions`` returns the prepared report of ``env.reports``."""

    def __init__(self, env: Env) -> None:
        self.env = env
        self.calls: list[int] = []
        self.require_calls: list[tuple[int, str]] = []

    async def get_snapshot(self, telegram_user_id: int) -> PermissionSnapshot:
        return self.env.snapshot

    async def effective_permissions(self, telegram_user_id: int) -> Any:
        self.calls.append(telegram_user_id)
        return self.env.reports[telegram_user_id]

    async def require(self, telegram_user_id: int, slug: str, tenant_id: UUID | None = None) -> Any:
        self.require_calls.append((telegram_user_id, slug))
        decision = self.env.snapshot.decide(slug, tenant_id)
        if not decision.allowed:
            raise PermissionError(slug)
        return decision


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
        roles: list[SimpleNamespace] | None = None,
        activity_repository: Any | None = None,
        export_service: Any | None = None,
        filter_options: Any | None = None,
        master_telegram_bot_id: int | None = None,
        bot_control_service: Any | None = None,
        click_log: Any | None = None,
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
            set_timezone=lambda actor, tenant_id, timezone: self._set(tenant_id, timezone=timezone),
            restore=lambda actor, tenant_id: self._set(tenant_id, is_deleted=False),
            deactivate=lambda actor, tenant_id: self._set(tenant_id, is_active=False),
            update_settings=self._update_settings,
        )
        self.user_service = Recorder(add_by_telegram_id=self._add_user)
        self.access = Recorder()
        self.role_store: dict[UUID, SimpleNamespace] = {r.id: r for r in roles or []}
        self.user_roles: dict[UUID, set[UUID]] = {}
        self.reports: dict[int, Any] = {}
        self.live_slugs: frozenset[str] = SEED_PERMISSION_SLUGS
        self.permissions = FakePermissionService(self)
        self.overrides: dict[UUID, list[Any]] = {}
        self.role_perms: dict[UUID, frozenset[str]] = {r.id: frozenset() for r in roles or []}
        self.roles = Recorder(
            create=self._create_role,
            rename=lambda actor, role_id, name: self._set_role(role_id, name=name),
            activate=lambda actor, role_id: self._set_role(role_id, is_active=True),
            deactivate=lambda actor, role_id: self._set_role(role_id, is_active=False),
            soft_delete=lambda actor, role_id: self._set_role(role_id, is_deleted=True),
            set_permissions=self._set_role_permissions,
        )
        self.queries = FakeQueries(self)
        self.global_access: set[int] = set()
        self.system_settings = SimpleNamespace(
            is_maintenance_mode=False,
            iam_cache_ttl_seconds=86400,
            user_activity_retention_months=3,
            audit_logs_retention_months=24,
            file_logs_retention_days=90,
        )
        settings_row = self.system_settings
        self.system = Recorder(
            get=lambda: settings_row,
            set_maintenance=lambda actor, on: setattr(settings_row, "is_maintenance_mode", on),
            set_iam_cache_ttl=lambda actor, v: setattr(settings_row, "iam_cache_ttl_seconds", v),
            set_retention=self._set_retention,
            set_file_logs_retention_days=lambda actor, v: setattr(
                settings_row, "file_logs_retention_days", v
            ),
        )
        from app.modules.bot_manager.service import BotControlService

        container = FakeContainer(
            {
                BotControlService: bot_control_service,
                TenantAdminService: self.tenants,
                UserAdminService: self.user_service,
                AccessAdminService: self.access,
                RoleAdminService: self.roles,
                PermissionService: self.permissions,
                IamAdminQueries: self.queries,
                SystemSettingsService: self.system,
                ActivityQueryRepository: activity_repository,
                ExportService: export_service,
                ActivityFilterOptions: filter_options,
            }
        )

        async def inject(
            handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
            event: Any,
            data: dict[str, Any],
        ) -> Any:
            data[SNAPSHOT_KEY] = self.snapshot
            data[CONTAINER_NAME] = container
            data["master_telegram_bot_id"] = master_telegram_bot_id
            return await handler(event, data)

        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.update.outer_middleware(inject)
        if click_log is not None:
            dispatcher.update.outer_middleware(click_log)
        dispatcher.include_router(build_menu_router())
        self.messages = CheckedMessageManager()  # type: ignore[no-untyped-call]
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

    def _create_role(self, actor: int, *, name: str, slug: str) -> SimpleNamespace:
        role = make_role(name, slug)
        self.role_store[role.id] = role
        self.role_perms[role.id] = frozenset()
        return role

    def _set_role(self, role_id: UUID, **values: Any) -> SimpleNamespace:
        role = self.role_store[role_id]
        for key, value in values.items():
            setattr(role, key, value)
        return role

    def _set_role_permissions(self, actor: int, role_id: UUID, slugs: Any) -> frozenset[str]:
        self.role_perms[role_id] = frozenset(slugs)
        return self.role_perms[role_id]

    def _set_retention(
        self,
        actor: int,
        user_activity_months: int | None = None,
        audit_logs_months: int | None = None,
    ) -> SimpleNamespace:
        if user_activity_months is not None:
            self.system_settings.user_activity_retention_months = user_activity_months
        if audit_logs_months is not None:
            self.system_settings.audit_logs_retention_months = audit_logs_months
        return self.system_settings

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
