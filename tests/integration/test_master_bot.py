"""Integration: Master Bot permission loader / username sync on PostgreSQL + Redis DB 15 and
click-log through ``LogBuffer`` into ``user_activity`` and files (task 04)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.types import Chat, Message, Update, User
from dishka import AsyncContainer, make_async_container
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.entrypoints.master_bot.app import permission_loader, username_sync
from app.entrypoints.master_bot.middlewares import (
    SNAPSHOT_KEY,
    TEXT_MARKER,
    ClickLogMiddleware,
)
from app.modules.iam.admin_service import (
    AccessAdminService,
    RoleAdminService,
    TenantAdminService,
)
from app.modules.iam.ioc import IamProvider
from app.modules.iam.models import Permission, Tenant, TenantSettings
from app.modules.iam.models import User as IamUser
from app.modules.iam.permissions import (
    LOGS_VIEW,
    ORDERS_MANAGE,
    PRODUCTS_CREATE,
    PRODUCTS_VIEW,
    USERS_VIEW,
)
from app.modules.iam.presentation.tg_master.effective import general_getter
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.iam.presentation.tg_master.states import TenantsSG
from app.modules.iam.presentation.tg_master.tenants import card_getter, on_slug
from app.modules.iam.snapshot import PermissionSnapshot
from app.modules.logging.buffer import LogBuffer
from app.modules.logging.files import DailyFileWriter
from app.modules.logging.metrics import LogsDroppedCounter
from app.modules.logging.models import UserActivity
from app.modules.system.ioc import SystemProvider
from app.shared_kernel.domain.exceptions import DomainError
from app.shared_kernel.infrastructure.database import build_session_factory
from app.shared_kernel.infrastructure.providers import DatabaseProvider, RedisProvider
from app.shared_kernel.presentation.tg_master.common import (
    CONTAINER_NAME,
)
from tests.integration.test_permission_service import grant_role

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.3", "S-II.2")]

BOT_TOKEN = "123456789:AAH-secretTokenValue_abcdefghijklmnop"


def _tg() -> int:
    return uuid4().int >> 80


@pytest.fixture
async def container(settings: Settings, clean_db: AsyncEngine) -> AsyncIterator[AsyncContainer]:
    built = make_async_container(
        DatabaseProvider(),
        RedisProvider(),
        IamProvider(),
        SystemProvider(),
        context={Settings: settings},
    )
    try:
        yield built
    finally:
        await built.close()


async def _create_users(engine: AsyncEngine) -> dict[str, int]:
    ids = {name: _tg() for name in ("superuser", "admin", "inactive", "plain")}
    factory = build_session_factory(engine)
    async with factory() as session:
        session.add(IamUser(telegram_user_id=ids["superuser"], is_superuser=True))
        session.add(IamUser(telegram_user_id=ids["inactive"], is_active=False, is_superuser=True))
        session.add(IamUser(telegram_user_id=ids["plain"]))
        admin = IamUser(telegram_user_id=ids["admin"], telegram_user_name="old")
        session.add(admin)
        await session.flush()
        await grant_role(session, admin, "admin")
        await session.commit()
    return ids


async def test_permission_loader_real_db(
    container: AsyncContainer, clean_db: AsyncEngine, redis_client: Any
) -> None:
    ids = await _create_users(clean_db)
    load = permission_loader(container)
    assert not (await load(_tg())).exists  # unknown
    assert not (await load(ids["inactive"])).exists
    assert (await load(ids["superuser"])).is_superuser
    admin = await load(ids["admin"])
    assert admin.exists and admin.decide(USERS_VIEW).allowed
    plain = await load(ids["plain"])
    assert plain.exists and not plain.decide(USERS_VIEW).allowed


async def test_username_sync_real_db(
    container: AsyncContainer, clean_db: AsyncEngine, redis_client: Any
) -> None:
    ids = await _create_users(clean_db)
    await username_sync(container)(ids["admin"], "new_name")
    async with build_session_factory(clean_db)() as session:
        name = await session.scalar(
            select(IamUser.telegram_user_name).where(IamUser.telegram_user_id == ids["admin"])
        )
    assert name == "new_name"


async def test_click_log_reaches_db_and_files_without_text(
    clean_db: AsyncEngine, tmp_path: Path
) -> None:
    factory = build_session_factory(clean_db)
    buffer = LogBuffer(
        factory,
        DailyFileWriter(tmp_path),
        batch_size=10,
        flush_interval_seconds=0.05,
        prune_interval_seconds=None,
        counter=LogsDroppedCounter(),
    )
    bot_id = uuid4().int >> 66
    user = User(id=_tg(), is_bot=False, first_name="a", username="staff")
    chat = Chat(id=user.id, type="private")
    middleware = ClickLogMiddleware(buffer, telegram_bot_id=bot_id)

    async def handler(event: Any, data: dict[str, Any]) -> None:
        return None

    await buffer.start()
    for update_id, text in enumerate(("/start", f"мой токен {BOT_TOKEN}")):
        message = Message(
            message_id=update_id, date=datetime.now(UTC), chat=chat, from_user=user, text=text
        )
        update = Update(update_id=update_id, message=message)
        await middleware(handler, update, {"event_from_user": user, "event_chat": chat})
    await buffer.stop(10)

    async with factory() as session:
        rows = (
            await session.execute(
                select(UserActivity.action_type, UserActivity.button, UserActivity.tenant_id)
                .where(UserActivity.telegram_bot_id == bot_id)
                .order_by(UserActivity.action_type)
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        ("command", "/start", None),
        ("message", TEXT_MARKER, None),
    ]
    contents = _read_files(tmp_path)
    assert contents
    assert all(BOT_TOKEN not in text for text in contents)


def _read_files(root: Path) -> list[str]:
    return [path.read_text(encoding="utf-8") for path in root.rglob("*") if path.is_file()]


def _manager(request: AsyncContainer, actor: int) -> Any:
    """The part of ``DialogManager`` used by the IAM handlers/getters."""
    return SimpleNamespace(
        middleware_data={
            CONTAINER_NAME: request,
            SNAPSHOT_KEY: PermissionSnapshot(user_id=uuid4(), is_superuser=True),
            "event_from_user": User(id=actor, is_bot=False, first_name="s"),
        },
        dialog_data={"new_name": "Dialog Shop"},
        switch_to=AsyncMock(),
    )


@pytest.mark.req("R-II.4")
async def test_dialog_creates_tenant_real_db(
    container: AsyncContainer, clean_db: AsyncEngine, redis_client: Any
) -> None:
    """Slug step handler -> real ``TenantAdminService`` (DI REQUEST scope) -> card."""
    ids = await _create_users(clean_db)
    slug = f"dialog-{uuid4().hex[:8]}"
    async with container() as request:
        manager = _manager(request, ids["superuser"])
        await on_slug(AsyncMock(), None, manager, slug)
        manager.switch_to.assert_awaited_once_with(TenantsSG.card)
        card = await card_getter(manager)
    assert "Dialog Shop" in card["text"] and slug in card["text"]
    async with build_session_factory(clean_db)() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
        assert tenant is not None and str(tenant.id) == manager.dialog_data["tenant_id"]
        assert await session.scalar(
            select(TenantSettings.id).where(TenantSettings.tenant_id == tenant.id)
        )


@pytest.mark.req("R-II.4")
async def test_dialog_reads_leave_session_clean(
    container: AsyncContainer, clean_db: AsyncEngine, redis_client: Any
) -> None:
    """Reads of ``IamAdminQueries`` before a mutation in the same request (on_flag/on_tenant)
    leave no pending objects, so the service does not refuse with ``session_not_clean``."""
    ids = await _create_users(clean_db)
    async with container() as request:
        service = await request.get(TenantAdminService)
        tenant = await service.create(ids["superuser"], name="Shop", slug=f"s-{uuid4().hex[:8]}")
        queries = await request.get(IamAdminQueries)
        session = await request.get(AsyncSession)
        found = await queries.tenant(tenant.id)
        assert found is not None and found[1] is not None
        user = await queries.user(ids["admin"])
        assert user is not None
        await queries.user_roles(user.id)
        await queries.user_tenants(user.id)
        await queries.users()
        await queries.live_languages()
        await queries.deleted_tenants()
        assert not session.new and not session.dirty and not session.deleted
        before = found[1].is_store_open
        settings = await service.update_settings(
            ids["superuser"], tenant.id, {"is_store_open": not before}
        )
        assert settings.is_store_open is (not before)


@pytest.mark.req("R-II.4")
async def test_effective_permissions_view_real_db(
    container: AsyncContainer, clean_db: AsyncEngine, redis_client: Any
) -> None:
    """Role + assignment + override through the real services, then the read queries and
    the effective permissions getter of the dialog show the sources."""
    ids = await _create_users(clean_db)
    actor, target = ids["superuser"], ids["plain"]
    until = datetime.now(UTC).replace(microsecond=0) + timedelta(days=5)
    async with container() as request:
        roles = await request.get(RoleAdminService)
        role = await roles.create(actor, name="Поддержка", slug=f"support_{uuid4().hex[:6]}")
        role_id = role.id
        await roles.set_permissions(actor, role_id, {PRODUCTS_VIEW})
    async with container() as request:
        access = await request.get(AccessAdminService)
        await access.assign_role(actor, target, role_id)
        await access.upsert_override(
            actor, target, LOGS_VIEW, is_granted=True, expires_at=until, reason="дежурство"
        )
    async with container() as request:
        queries = await request.get(IamAdminQueries)
        user = await queries.user(target)
        assert user is not None
        assert await queries.user_role_ids(user.id) == frozenset({role_id})
        (row,) = await queries.user_overrides(user.id)
        assert row.permission_slug == LOGS_VIEW and not row.is_expired
        assert row.granted_by_telegram_id == actor and row.override.reason == "дежурство"
        assert await queries.user_brief(user.id) == (target, None)
        manager = _manager(request, actor)
        manager.start_data = {"user_id": str(target)}
        manager.dialog_data = {}
        page = await general_getter(manager)
    text = page["text"]
    assert "Роли: Поддержка" in text
    assert "✅ Просмотр товаров — роль: Поддержка" in text
    assert "✅ Просмотр логов — персонально: разрешено" in text
    assert f"выдал: {actor}" in text and "дежурство" in text


async def _set_active(engine: AsyncEngine, slugs: list[str], *, active: bool) -> None:
    async with build_session_factory(engine)() as session:
        await session.execute(
            update(Permission).where(Permission.slug.in_(slugs)).values(is_active=active)
        )
        await session.commit()


@pytest.mark.req("R-II.4")
async def test_set_permissions_keeps_held_inactive_permission_real_db(
    container: AsyncContainer, clean_db: AsyncEngine, redis_client: Any
) -> None:
    """Saving a role keeps a held inactive permission; adding a new inactive one fails."""
    ids = await _create_users(clean_db)
    actor = ids["superuser"]
    async with container() as request:
        role = await (await request.get(RoleAdminService)).create(
            actor,
            name="Legacy",
            slug=f"legacy_{uuid4().hex[:6]}",
            permission_slugs={PRODUCTS_VIEW, LOGS_VIEW},
        )
        role_id = role.id
    await _set_active(clean_db, [LOGS_VIEW, ORDERS_MANAGE], active=False)
    try:
        async with container() as request:
            after = await (await request.get(RoleAdminService)).set_permissions(
                actor, role_id, {PRODUCTS_VIEW, PRODUCTS_CREATE, LOGS_VIEW}
            )
        assert after == frozenset({PRODUCTS_VIEW, PRODUCTS_CREATE, LOGS_VIEW})
        async with container() as request:
            with pytest.raises(DomainError) as error:
                await (await request.get(RoleAdminService)).set_permissions(
                    actor, role_id, {PRODUCTS_VIEW, LOGS_VIEW, ORDERS_MANAGE}
                )
        assert error.value.code == "permission_not_found"
    finally:
        await _set_active(clean_db, [LOGS_VIEW, ORDERS_MANAGE], active=True)
