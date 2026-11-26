"""Tenant lifecycle, committed STOP commands and delivery for R-II.16."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from aiogram.types import CallbackQuery
from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.kbd import Button
from faststream.rabbit import RabbitBroker, RabbitQueue, TestRabbitBroker
from faststream_outbox.retry import ExponentialRetry
from psycopg import Error as PsycopgError
from sqlalchemy import Connection, Select, event, select, text, update
from sqlalchemy.engine.interfaces import DBAPICursor, ExecutionContext
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.sql import Executable

from app.core.config import ProxySettings
from app.core.i18n import gettext
from app.entrypoints.master_bot.middlewares import SNAPSHOT_KEY
from app.modules.bot_manager.commands import BOT_COMMAND_FANOUT, BotCommand
from app.modules.bot_manager.models import Bot, Proxy
from app.modules.bot_manager.proxy_repository import ProxyRepository
from app.modules.bot_manager.proxy_service import ProxyPoolService
from app.modules.bot_manager.publisher import BotCommandPublisher
from app.modules.bot_manager.security import token_hash
from app.modules.bot_manager.service import BotControlService, TenantBotsStopResult
from app.modules.iam.admin_service import TenantAdminService
from app.modules.iam.models import Tenant, User
from app.modules.iam.permissions import BOTS_MANAGE, TENANTS_MANAGE
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.iam.presentation.tg_master.tenants import on_activate
from app.modules.iam.service import PermissionService
from app.modules.iam.snapshot import PermissionSnapshot
from app.modules.logging.models import AuditLog
from app.shared_kernel.domain.exceptions import DomainError
from app.shared_kernel.infrastructure.outbox import (
    OUTBOX_QUEUE,
    SharedOutboxRelay,
    build_outbox_broker,
    outbox_dlq,
    outbox_messages,
)
from app.shared_kernel.presentation.tg_master.common import CONTAINER_NAME, NOTICE_KEY

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.16")]
ACTOR = 42
BEFORE = datetime(2020, 1, 1, tzinfo=UTC)


class Permissions:
    def __init__(self, *, manage_tenants: bool = True, bot_scope: UUID | None = None) -> None:
        self.manage_tenants = manage_tenants
        self.bot_scope = bot_scope

    async def require(self, actor: int, slug: str, tenant_id: UUID | None = None) -> None:
        assert actor == ACTOR
        if slug == TENANTS_MANAGE and tenant_id is None and self.manage_tenants:
            return
        if slug == BOTS_MANAGE and self.bot_scope is not None and tenant_id == self.bot_scope:
            return
        raise DomainError("Denied", code="permission_denied")

    async def invalidate_all(self) -> SimpleNamespace:
        return SimpleNamespace(ok=True)


@dataclass(frozen=True)
class Shops:
    factory: async_sessionmaker[AsyncSession]
    tenant: UUID
    foreign: UUID
    proxy: UUID
    enabled: UUID
    disabled: UUID
    deleted: UUID
    other: UUID
    token: str
    telegram_id: int


@pytest.fixture
async def shops(clean_db: AsyncEngine) -> Shops:
    factory = async_sessionmaker(clean_db, expire_on_commit=False)
    async with factory() as session:
        tenant = Tenant(name="Target shop", slug="target-shop", is_active=True)
        foreign = Tenant(name="Other shop", slug="other-shop", is_active=True)
        proxy = Proxy(url="socks5://proxy:1080", url_hash=uuid4().hex)
        session.add_all([tenant, foreign, proxy, User(telegram_user_id=ACTOR, is_active=True)])
        await session.flush()
        rows = []
        for n, owner, active, deleted in (
            (100001, tenant, True, False),
            (100002, tenant, False, False),
            (100003, tenant, False, True),
            (100004, foreign, True, False),
        ):
            token = f"{n}:" + "A" * 35
            rows.append(
                Bot(
                    tenant_id=owner.id,
                    telegram_bot_id=n,
                    encrypted_token=token,
                    token_hash=token_hash(token),
                    is_active=active,
                    is_deleted=deleted,
                    status="running" if active else "stopped",
                    status_changed_at=BEFORE,
                    proxy_id=proxy.id if n == 100001 else None,
                )
            )
        session.add_all(rows)
        await session.commit()
    return Shops(
        factory=factory,
        tenant=tenant.id,
        foreign=foreign.id,
        proxy=proxy.id,
        enabled=rows[0].id,
        disabled=rows[1].id,
        deleted=rows[2].id,
        other=rows[3].id,
        token=rows[0].encrypted_token,
        telegram_id=rows[0].telegram_bot_id,
    )


def services(
    session: AsyncSession, engine: AsyncEngine
) -> tuple[TenantAdminService, BotControlService, ProxyPoolService]:
    permissions = Permissions()
    pool = ProxyPoolService(session, ProxySettings(proxy_max_bots_per_ip=1), AsyncMock())
    control = BotControlService(
        session,
        permissions=permissions,
        proxy_pool=pool,
        publisher=BotCommandPublisher(build_outbox_broker(engine)),
    )
    # The IAM service uses only require() and invalidate_all() in these scenarios.
    return TenantAdminService(session, cast(PermissionService, permissions)), control, pool


async def commands(session: AsyncSession) -> list[BotCommand]:
    return [
        BotCommand.model_validate_json(payload)
        for payload in await session.scalars(
            select(outbox_messages.c.payload).where(outbox_messages.c.queue == OUTBOX_QUEUE)
        )
    ]


def stop_audit() -> Select[AuditLog]:
    return select(AuditLog).where(AuditLog.action == "bot.stop")


def sqlstate(exc: DBAPIError) -> str | None:
    original = exc.orig
    assert isinstance(original, PsycopgError)
    return original.sqlstate


@pytest.mark.parametrize("delete", [False, True], ids=["deactivate-activate", "delete-restore"])
async def test_tenant_stop_and_make_runnable_again(
    clean_db: AsyncEngine, shops: Shops, delete: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    reason: Literal["tenant_deleted", "tenant_inactive"] = (
        "tenant_deleted" if delete else "tenant_inactive"
    )
    async with shops.factory() as writer, shops.factory() as reader:
        admin, control, pool = services(writer, clean_db)
        if delete:
            # Remove ambiguity: restore must activate, not just clear is_deleted.
            await admin.deactivate(ACTOR, shops.tenant)
            await admin.soft_delete(ACTOR, shops.tenant)
        else:
            await admin.deactivate(ACTOR, shops.tenant)
        assert await commands(reader) == []
        assert await control.stop_tenant(
            shops.tenant, reason=reason, actor=ACTOR
        ) == TenantBotsStopResult(1, int(delete))
        bots = {bot.id: bot for bot in await reader.scalars(select(Bot))}
        assert set(bots) == {shops.enabled, shops.disabled, shops.deleted, shops.other}
        for bot_id, active, proxy_id in (
            (shops.enabled, True, None if delete else shops.proxy),
            (shops.disabled, False, None),
        ):
            bot = bots[bot_id]
            assert (bot.status, bot.last_error, bot.is_active, bot.is_deleted, bot.proxy_id) == (
                "stopped",
                reason,
                active,
                False,
                proxy_id,
            )
            if active:
                assert bot.status_changed_at is not None and bot.status_changed_at > BEFORE
            else:
                # Updating the reason alone is not a status transition.
                assert bot.status_changed_at == BEFORE
        assert (bots[shops.enabled].encrypted_token, bots[shops.enabled].telegram_bot_id) == (
            shops.token,
            shops.telegram_id,
        )
        assert (
            bots[shops.deleted].is_deleted,
            bots[shops.deleted].last_error,
            bots[shops.deleted].status_changed_at,
        ) == (True, None, BEFORE)
        assert (
            bots[shops.other].status,
            bots[shops.other].last_error,
            bots[shops.other].status_changed_at,
        ) == ("running", None, BEFORE)
        assert {bot.id for bot in await control.repo.list_runnable()} == {shops.other}
        available = await pool.get_available_proxy()
        assert (available.id if available else None) == (shops.proxy if delete else None)

        queued = await commands(reader)
        assert len(queued) == 2
        assert {(c.kind, c.bot_id, c.issued_by) for c in queued} == {
            ("STOP_BOT", bot_id, ACTOR) for bot_id in (shops.enabled, shops.disabled)
        }
        assert await control.stop_tenant(
            shops.tenant, reason=reason, actor=ACTOR
        ) == TenantBotsStopResult(0, 0)
        event = (await reader.scalars(stop_audit())).one()
        assert (event.entity_id, event.tenant_id, event.actor_id, event.payload) == (
            shops.enabled,
            shops.tenant,
            ACTOR,
            {"reason": reason},
        )

        if delete:
            # New token hash, same Telegram identity: uniqueness is not just token-based.
            control.permissions = Permissions(manage_tenants=False, bot_scope=shops.foreign)
            monkeypatch.setattr(
                control,
                "_get_me",
                AsyncMock(
                    return_value=SimpleNamespace(
                        id=shops.telegram_id, username="example_bot", first_name="Example"
                    )
                ),
            )
            with pytest.raises(DomainError) as duplicate:
                await control.add_bot(shops.foreign, f"{shops.telegram_id}:" + "B" * 35, ACTOR)
            assert duplicate.value.code == "bot_in_deleted_tenant"
            tenant = await admin.restore(ACTOR, shops.tenant)
        else:
            tenant = await admin.activate(ACTOR, shops.tenant)
        assert tenant.is_active and not tenant.is_deleted
        assert {bot.id for bot in await control.repo.list_runnable()} == {
            shops.enabled,
            shops.other,
        }


async def test_enabled_bot_count_rechecks_committed_tenant_state(
    clean_db: AsyncEngine, shops: Shops
) -> None:
    async with shops.factory() as reader, shops.factory() as writer:
        _, control, _ = services(reader, clean_db)
        admin, _, _ = services(writer, clean_db)
        cached = await reader.get(Tenant, shops.tenant)
        assert cached is not None and cached.is_active
        await reader.commit()  # Keep the active ORM object, but leave the session idle.
        assert await control.count_enabled_for_tenant(shops.tenant) == 1
        assert not reader.in_transaction()

        await admin.deactivate(ACTOR, shops.tenant)
        assert cached.is_active  # Still stale in the reader's identity map.
        assert await control.count_enabled_for_tenant(shops.tenant) == 0
        assert not reader.in_transaction()

        await admin.activate(ACTOR, shops.tenant)
        await admin.soft_delete(ACTOR, shops.tenant)
        assert await control.count_enabled_for_tenant(shops.tenant) == 0


async def test_enabled_bot_count_excludes_active_soft_deleted_tenant(
    clean_db: AsyncEngine, shops: Shops
) -> None:
    async with shops.factory() as writer, shops.factory() as reader:
        _, control, _ = services(reader, clean_db)
        assert await control.count_enabled_for_tenant(shops.tenant) == 1
        await writer.execute(
            update(Tenant).where(Tenant.id == shops.tenant).values(is_active=True, is_deleted=True)
        )
        await writer.commit()

        assert await control.count_enabled_for_tenant(shops.tenant) == 0
        assert not reader.in_transaction()


async def test_optional_count_preserves_callers_open_transaction(
    clean_db: AsyncEngine, shops: Shops
) -> None:
    async with shops.factory() as writer:
        _, control, _ = services(writer, clean_db)
        shop_name = select(Tenant.name).where(Tenant.id == shops.tenant)
        transaction = await writer.begin()
        try:
            await writer.execute(
                update(Tenant).where(Tenant.id == shops.tenant).values(name="Uncommitted name")
            )
            assert await control.count_enabled_for_tenant(shops.tenant) == 0
            assert writer.get_transaction() is transaction
            assert await writer.scalar(shop_name) == "Uncommitted name"
        finally:
            await writer.rollback()
        assert await writer.scalar(shop_name) == "Target shop"


async def test_failed_optional_count_keeps_activation_notice_and_session(
    clean_db: AsyncEngine, shops: Shops, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with shops.factory() as writer, shops.factory() as reader:
        admin, control, _ = services(writer, clean_db)
        await admin.deactivate(ACTOR, shops.tenant)
        dependencies: dict[type, object] = {
            TenantAdminService: admin,
            BotControlService: control,
            IamAdminQueries: IamAdminQueries(writer),
        }

        async def resolve(kind: type) -> object:
            return dependencies[kind]

        manager = SimpleNamespace(
            dialog_data={"tenant_id": str(shops.tenant)},
            middleware_data={
                CONTAINER_NAME: SimpleNamespace(get=resolve),
                SNAPSHOT_KEY: PermissionSnapshot(user_id=uuid4(), is_superuser=True),
                "event_from_user": SimpleNamespace(id=ACTOR),
            },
        )
        scalar = writer.scalar
        sqlstates: list[str | None] = []

        async def fail_count(statement: Executable, *args: Any, **kwargs: Any) -> Any:
            if str(statement).lower().startswith("select count("):
                # A real PostgreSQL error aborts the read transaction until rollback.
                try:
                    return await scalar(text("SELECT 1 / 0"))
                except DBAPIError as exc:
                    sqlstates.append(sqlstate(exc))
                    raise
            return await scalar(statement, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(writer, "scalar", fail_count)
            # This handler ignores the callback and widget, and only reads the manager's data.
            await on_activate(
                cast(CallbackQuery, None), cast(Button, None), cast(DialogManager, manager)
            )
        assert sqlstates == ["22012"]
        assert manager.dialog_data[NOTICE_KEY] == gettext("Готово.")
        assert not writer.in_transaction()
        tenant = await reader.get(Tenant, shops.tenant)
        assert tenant is not None and tenant.is_active
        assert await control.count_enabled_for_tenant(shops.tenant) == 1
        assert not writer.in_transaction()


async def test_stop_requires_tenants_manage_even_with_bots_manage(
    clean_db: AsyncEngine, shops: Shops
) -> None:
    async with shops.factory() as writer, shops.factory() as reader:
        admin, control, _ = services(writer, clean_db)
        await admin.deactivate(ACTOR, shops.tenant)
        control.permissions = Permissions(manage_tenants=False, bot_scope=shops.tenant)
        with pytest.raises(DomainError) as denied:
            await control.stop_tenant(shops.tenant, reason="tenant_inactive", actor=ACTOR)
        assert denied.value.code == "permission_denied"
        bot = await reader.get(Bot, shops.enabled)
        assert bot is not None and bot.status == "running"
        assert await commands(reader) == []
        assert await reader.scalar(stop_audit()) is None


async def test_enqueue_failure_rolls_back_bots_not_tenant(
    clean_db: AsyncEngine, shops: Shops, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with shops.factory() as writer, shops.factory() as reader:
        admin, control, pool = services(writer, clean_db)
        await admin.soft_delete(ACTOR, shops.tenant)
        publisher = control.publisher
        assert publisher is not None
        publish = publisher.publish_transactional

        async def fail_after_append(
            command: BotCommand, session: AsyncSession, *, traceparent: str | None = None
        ) -> None:
            await publish(command, session, traceparent=traceparent)
            queued = await commands(session)
            assert await commands(reader) == []
            if len(queued) == 2:
                raise RuntimeError("enqueue failed after append")

        monkeypatch.setattr(publisher, "publish_transactional", fail_after_append)
        with pytest.raises(RuntimeError, match="enqueue failed after append"):
            await control.stop_tenant(shops.tenant, reason="tenant_deleted", actor=ACTOR)
        tenant = await reader.get(Tenant, shops.tenant)
        assert tenant is not None and tenant.is_deleted
        bot = await reader.get(Bot, shops.enabled)
        assert bot is not None
        assert (bot.status, bot.last_error, bot.proxy_id, bot.status_changed_at) == (
            "running",
            None,
            shops.proxy,
            BEFORE,
        )
        disabled = await reader.get(Bot, shops.disabled)
        assert disabled is not None and disabled.last_error is None
        assert await commands(reader) == []
        assert await reader.scalar(stop_audit()) is None
        assert await pool.get_available_proxy() is None


@pytest.mark.parametrize("size", [1, 3])
async def test_release_bots_sends_one_uuid_array_bind(
    clean_db: AsyncEngine, shops: Shops, size: int
) -> None:
    assert clean_db.dialect.driver == "psycopg"
    ids = {shops.enabled} if size == 1 else {shops.enabled, shops.disabled, shops.deleted}
    async with clean_db.begin() as setup:
        await setup.execute(update(Bot).where(Bot.id.in_(ids)).values(proxy_id=shops.proxy))

    updates: list[tuple[str, dict[str, object]]] = []

    def capture_update(
        conn: Connection,
        cursor: DBAPICursor,
        statement: str,
        parameters: object,
        context: ExecutionContext | None,
        executemany: bool,
    ) -> None:
        sql = " ".join(statement.lower().split())
        if sql.startswith("update bots "):
            assert not executemany
            assert isinstance(parameters, Mapping)
            updates.append((sql, dict(parameters)))

    async with shops.factory.begin() as writer:
        repo = ProxyRepository(writer)
        assert await repo.get(shops.proxy, for_update=True) is not None
        connection = (await writer.connection()).sync_connection
        event.listen(connection, "before_cursor_execute", capture_update)
        try:
            assert await repo.release_bots(shops.proxy) == tuple(sorted(ids))
        finally:
            event.remove(connection, "before_cursor_execute", capture_update)

    [(sql, parameters)] = updates
    where = sql.split(" where ", 1)[1]
    assert re.search(r"bots\.proxy_id\s*=\s*%\([^)]+\)s", where)
    assert re.search(r"bots\.id\s*=\s*any\s*\(%\(locked_bot_ids\)s::uuid\[\]\)", where)
    # Two scalar proxy_id binds (SET and WHERE), no additional bind per bot ID.
    assert len(parameters) == 3
    assert parameters["locked_bot_ids"] == sorted(ids)
    async with clean_db.connect() as reader:
        bindings = await reader.execute(select(Bot.id, Bot.proxy_id).where(Bot.id.in_(ids)))
        assert set(bindings) == {(bot_id, None) for bot_id in ids}


async def test_proxy_ban_locks_proxy_then_bots_in_id_order(
    clean_db: AsyncEngine, shops: Shops
) -> None:
    """With the higher bot ID held, ban must already hold the proxy and the lower ID."""
    low, high = sorted((shops.enabled, shops.deleted))
    async with clean_db.begin() as setup:
        await setup.execute(update(Bot).where(Bot.id == shops.deleted).values(proxy_id=shops.proxy))

    async with (
        clean_db.connect() as blocker,
        clean_db.connect() as probe,
        shops.factory() as writer,
    ):
        blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        await blocker.execute(select(Bot.id).where(Bot.id == high).with_for_update())
        writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
        pool = ProxyPoolService(writer, ProxySettings(), AsyncMock(), permissions=AsyncMock())
        ban = asyncio.create_task(pool.ban(shops.proxy, ACTOR))
        try:
            # Poll PostgreSQL for the actual blocker, not elapsed time or an inferred plan.
            async with asyncio.timeout(5):
                while True:
                    if ban.done():
                        pytest.fail(f"Ban finished while the bot was locked: {ban.result()}")
                    query = await probe.scalar(
                        text(
                            "SELECT query FROM pg_stat_activity WHERE pid = :writer "
                            "AND :blocker = ANY(pg_blocking_pids(pid))"
                        ),
                        {"writer": writer_pid, "blocker": blocker_pid},
                    )
                    await probe.rollback()  # Refresh pg_stat_activity on the next poll.
                    if query is not None:
                        break
            # Reject an UPDATE that happens to lock rows in the same order on this DB.
            locking_sql = " ".join(query.lower().split())
            assert locking_sql.startswith("select bots.id ")
            assert "order by bots.id for update" in locking_sql

            for table, row_id in ((Proxy, shops.proxy), (Bot, low)):
                async with probe.begin():
                    with pytest.raises(DBAPIError) as locked:
                        await probe.execute(
                            select(table.id).where(table.id == row_id).with_for_update(nowait=True)
                        )
                    assert sqlstate(locked.value) == "55P03"
                    await probe.rollback()

            await blocker.rollback()
            assert await asyncio.wait_for(ban, timeout=5) == (low, high)
            bindings = await probe.execute(
                select(Bot.id, Bot.proxy_id, Bot.is_deleted).where(Bot.id.in_((low, high)))
            )
            assert set(bindings) == {(shops.enabled, None, False), (shops.deleted, None, True)}
        finally:
            if not ban.done():
                ban.cancel()
            await asyncio.gather(ban, return_exceptions=True)


@pytest.mark.parametrize("delete", [False, True], ids=["deactivate", "soft-delete"])
async def test_stop_tenant_locks_live_bots_in_id_order(
    clean_db: AsyncEngine, shops: Shops, delete: bool
) -> None:
    low, high = sorted((shops.enabled, shops.disabled))
    reason: Literal["tenant_deleted", "tenant_inactive"] = (
        "tenant_deleted" if delete else "tenant_inactive"
    )
    async with (
        clean_db.connect() as blocker,
        clean_db.connect() as probe,
        shops.factory() as writer,
    ):
        admin, control, _ = services(writer, clean_db)
        if delete:
            await admin.soft_delete(ACTOR, shops.tenant)
        else:
            await admin.deactivate(ACTOR, shops.tenant)
        blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        await blocker.execute(select(Bot.id).where(Bot.id == high).with_for_update())
        writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
        stop = asyncio.create_task(control.stop_tenant(shops.tenant, reason=reason, actor=ACTOR))
        try:
            async with asyncio.timeout(5):
                while True:
                    if stop.done():
                        pytest.fail(f"Stop finished while the bot was locked: {stop.result()}")
                    query = await probe.scalar(
                        text(
                            "SELECT query FROM pg_stat_activity WHERE pid = :writer "
                            "AND :blocker = ANY(pg_blocking_pids(pid))"
                        ),
                        {"writer": writer_pid, "blocker": blocker_pid},
                    )
                    await probe.rollback()  # Refresh pg_stat_activity on the next poll.
                    if query is not None:
                        break
            # Detect missing ORDER BY even if this plan happens to lock the lower ID first.
            locking_sql = " ".join(query.lower().split())
            assert "from bots " in locking_sql
            assert "order by bots.id for update" in locking_sql
            locks = [select(Bot.id).where(Bot.id == low)]
            if delete:
                # While waiting on the higher bot ID, stop must already hold the tenant's proxy.
                locks.append(select(Proxy.id).where(Proxy.id == shops.proxy))
            for statement in locks:
                with pytest.raises(DBAPIError) as locked:
                    await probe.execute(statement.with_for_update(nowait=True))
                assert sqlstate(locked.value) == "55P03"
                await probe.rollback()

            await blocker.rollback()
            assert await asyncio.wait_for(stop, timeout=5) == TenantBotsStopResult(1, int(delete))
            stopped = await probe.execute(
                select(Bot.id, Bot.status, Bot.last_error).where(Bot.id.in_((low, high)))
            )
            assert set(stopped) == {(bot_id, "stopped", reason) for bot_id in (low, high)}
        finally:
            if not stop.done():
                stop.cancel()
            await asyncio.gather(stop, return_exceptions=True)


async def test_stop_deleted_tenant_waits_for_proxy_before_locking_bots(
    clean_db: AsyncEngine, shops: Shops
) -> None:
    bot_ids = {shops.enabled, shops.disabled}
    async with (
        clean_db.connect() as blocker,
        clean_db.connect() as probe,
        shops.factory() as writer,
    ):
        admin, control, _ = services(writer, clean_db)
        await admin.soft_delete(ACTOR, shops.tenant)
        blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        locked_proxy = await blocker.scalar(
            select(Proxy.id).where(Proxy.id == shops.proxy).with_for_update()
        )
        assert locked_proxy == shops.proxy
        writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
        stop = asyncio.create_task(
            control.stop_tenant(shops.tenant, reason="tenant_deleted", actor=ACTOR)
        )
        try:
            async with asyncio.timeout(5):
                while True:
                    if stop.done():
                        pytest.fail(f"Stop finished while the proxy was locked: {stop.result()}")
                    query = await probe.scalar(
                        text(
                            "SELECT query FROM pg_stat_activity WHERE pid = :writer "
                            "AND :blocker = ANY(pg_blocking_pids(pid))"
                        ),
                        {"writer": writer_pid, "blocker": blocker_pid},
                    )
                    await probe.rollback()  # Refresh pg_stat_activity on the next poll.
                    if query is not None:
                        break
            # The blocker holds only the proxy row; the waiting statement must lock proxies.
            locking_sql = " ".join(query.lower().split())
            assert locking_sql.startswith("select proxies.id from proxies ")
            assert locking_sql.endswith("for update")
            locked_bots = await probe.scalars(
                select(Bot.id).where(Bot.id.in_(bot_ids)).with_for_update(nowait=True)
            )
            assert set(locked_bots) == bot_ids
            await probe.rollback()  # Release both bots while the proxy is still locked.

            await blocker.rollback()
            assert await asyncio.wait_for(stop, timeout=5) == TenantBotsStopResult(1, 1)
        finally:
            if not stop.done():
                stop.cancel()
            await asyncio.gather(stop, return_exceptions=True)


async def test_relay_retries_one_stop_without_blocking_the_other(
    clean_db: AsyncEngine, shops: Shops, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with shops.factory() as writer:
        admin, control, _ = services(writer, clean_db)
        await admin.deactivate(ACTOR, shops.tenant)
        await control.stop_tenant(shops.tenant, reason="tenant_inactive", actor=ACTOR)
    broker = RabbitBroker(logger=logging.Logger(__name__))
    deliveries: list[BotCommand] = []

    @broker.subscriber(RabbitQueue("task09b.stop"), BOT_COMMAND_FANOUT)
    async def consume(body: dict[str, object]) -> None:
        deliveries.append(BotCommand.model_validate(body))

    relay = SharedOutboxRelay(
        outbox_broker=build_outbox_broker(clean_db),
        rabbit_broker=broker,
        engine=clean_db,
        exchanges={OUTBOX_QUEUE: BOT_COMMAND_FANOUT},
        retry_strategy=ExponentialRetry(initial_delay_seconds=0.01, max_attempts=2),
    )
    async with TestRabbitBroker(broker):
        publish = broker.publish

        async def publish_or_fail(body: bytes, **kwargs: Any) -> Any:
            if BotCommand.model_validate_json(body).bot_id == shops.enabled:
                raise RuntimeError("simulated transport failure")
            return await publish(body, **kwargs)

        monkeypatch.setattr(broker, "publish", publish_or_fail)
        assert deliveries == []
        assert await relay.run_once() == 2
        assert [(c.kind, c.bot_id, c.issued_by) for c in deliveries] == [
            ("STOP_BOT", shops.disabled, ACTOR)
        ]
        async with clean_db.connect() as reader:
            pending = (await reader.execute(select(outbox_messages))).one()
            assert BotCommand.model_validate_json(pending.payload).bot_id == shops.enabled
            assert pending.deliveries_count == 1
        await asyncio.sleep(0.02)
        assert await relay.run_once() == 1
        assert len(deliveries) == 1
        async with clean_db.connect() as reader:
            assert (await reader.execute(select(outbox_messages))).first() is None
            dead = (await reader.execute(select(outbox_dlq))).one()
            assert (dead.original_id, dead.deliveries_count) == (pending.id, 2)
            assert BotCommand.model_validate_json(dead.payload).bot_id == shops.enabled
