"""Real PostgreSQL bot, audit and outbox transactions; Telegram is stubbed."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from aiogram.types import User
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import ProxySettings
from app.modules.bot_manager.models import Bot, Proxy
from app.modules.bot_manager.proxy_service import ProxyPoolService
from app.modules.bot_manager.publisher import BotCommandPublisher
from app.modules.bot_manager.security import token_hash
from app.modules.bot_manager.service import BotControlService
from app.modules.iam.models import Tenant
from app.modules.logging.models import AuditLog
from app.shared_kernel.domain.exceptions import DomainError
from app.shared_kernel.infrastructure.outbox import build_outbox_broker, outbox_messages

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.8")]
TOKEN = "123456:" + "A" * 35
NEW_TOKEN = "123456:" + "B" * 35
PROXY = "socks5://user:password@localhost:1080"


class ScopedPermissions:
    def __init__(self, *allowed: UUID) -> None:
        self.allowed = allowed

    async def require(self, actor: int, slug: str, tenant_id: UUID | None = None) -> None:
        assert actor == 42 and slug == "bots.manage"
        if tenant_id not in self.allowed:
            raise DomainError("Denied", code="permission_denied")


async def shop(session: AsyncSession, *, active: bool = True) -> UUID:
    tenant = Tenant(name="Shop", slug=f"shop-{uuid4().hex[:8]}", is_active=active)
    session.add(tenant)
    await session.commit()
    return tenant.id


@pytest.mark.parametrize("duplicate", [None, "same_token", "same_telegram_id"])
async def test_concurrent_proxy_capacity_race_retries_available_candidate(
    clean_db: AsyncEngine, duplicate: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = async_sessionmaker(clean_db, expire_on_commit=False)
    async with factory() as setup:
        tenant_ids = [await shop(setup), await shop(setup)]
        proxies = [
            Proxy(url=f"socks5://proxy{i}:1080", url_hash=uuid4().hex)
            for i in range(1 if duplicate else 2)
        ]
        setup.add_all(proxies)
        await setup.commit()
        proxy_ids = {p.id for p in proxies}
    barrier = asyncio.Barrier(2)
    calls: list[tuple[int, str | None]] = []

    async def run(index: int) -> UUID | None:
        async with factory() as session:
            pool = ProxyPoolService(session, ProxySettings(proxy_max_bots_per_ip=1), AsyncMock())
            service = BotControlService(
                session,
                permissions=ScopedPermissions(*tenant_ids),
                proxy_pool=pool,
                tenants_use_proxy=True,
            )

            async def get_me(token: str, url: str | None) -> User:
                calls.append((index, url))
                if sum(call[0] == index for call in calls) == 1:
                    await asyncio.wait_for(barrier.wait(), 5)
                return User(
                    is_bot=True,
                    id=100000 + (0 if duplicate else index),
                    username=f"bot{index}",
                    first_name="Bot",
                )

            monkeypatch.setattr(service, "_get_me", get_me)
            try:
                bot = await service.add_bot(
                    tenant_ids[index],
                    f"{100000 + (0 if duplicate else index)}:"
                    + ("B" if duplicate == "same_telegram_id" and index else "A") * 35,
                    42,
                )
            except DomainError as exc:
                assert duplicate and exc.code == "bot_already_exists"
                return None
            return bot.proxy_id

    assigned = await asyncio.wait_for(asyncio.gather(run(0), run(1)), 15)
    if duplicate:
        assert set(assigned) == proxy_ids | {None}
        assert len(calls) == 2
        return
    assert set(assigned) == proxy_ids
    assert len(calls) == 3
    assert sorted(sum(call[0] == i for call in calls) for i in (0, 1)) == [1, 2]
    assert calls[0][1] == calls[1][1]
    assert calls[2][1] != calls[0][1]


async def test_bot_pages_are_scoped_ordered_and_clamped(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime
    from uuid import UUID

    own, foreign = await shop(session), await shop(session)
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    service = BotControlService(session, permissions=ScopedPermissions(own))
    for i in reversed(range(11)):
        session.add(
            Bot(
                id=UUID(int=i + 1),
                tenant_id=foreign if i == 0 else own,
                telegram_bot_id=100000 + i,
                encrypted_token=TOKEN,
                token_hash=uuid4().hex,
                created_at=timestamp,
                is_deleted=i == 1,
            )
        )
    await session.commit()
    first = await service.page_for_tenant(42, own, page=-1)
    assert (first.index, first.page_count, first.read_only) == (0, 2, False)
    assert [bot.id.int for bot in first.items] == list(range(3, 11))
    last = await service.page_for_tenant(42, own, page=999)
    assert (last.index, last.page_count) == (1, 2)
    assert [bot.id.int for bot in last.items] == [11]
    for bot_id in (UUID(int=1), UUID(int=2)):
        with pytest.raises(DomainError) as error:
            await service.get_for_tenant(42, own, bot_id)
        assert error.value.code == "bot_not_found"
    with pytest.raises(DomainError) as error:
        await service.page_for_tenant(42, foreign, page=0)
    assert error.value.code == "permission_denied"
    visible = await service.get_for_tenant(42, own, UUID(int=11))
    visible.is_deleted = True
    await session.commit()
    clamped = await service.page_for_tenant(42, own, page=1)
    assert (clamped.index, clamped.page_count, len(clamped.items)) == (0, 1, 8)


async def test_page_snapshot_during_concurrent_delete(clean_db: AsyncEngine) -> None:
    factory = async_sessionmaker(clean_db, expire_on_commit=False)
    async with factory() as writer, factory() as reader:
        tenant = await shop(writer)
        bots = [
            Bot(
                tenant_id=tenant,
                telegram_bot_id=200000 + i,
                encrypted_token=TOKEN,
                token_hash=uuid4().hex,
            )
            for i in range(9)
        ]
        writer.add_all(bots)
        await writer.commit()
        service = BotControlService(reader, permissions=ScopedPermissions(tenant))
        last = await service.page_for_tenant(42, tenant, page=1)
        victim = await writer.get(Bot, last.items[0].id)
        assert victim is not None
        victim.is_deleted = True
        await writer.flush()
        # The concurrent writer has not committed: both parts of the page see the old row.
        before = await service.page_for_tenant(42, tenant, page=1)
        assert (before.index, before.page_count, [bot.id for bot in before.items]) == (
            1,
            2,
            [victim.id],
        )
        await writer.commit()
        after = await service.page_for_tenant(42, tenant, page=1)
        assert (after.index, after.page_count, len(after.items)) == (0, 1, 8)


async def test_proxy_add_duplicate_delete_and_restore(
    session: AsyncSession,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first, second = await shop(session), await shop(session)
    proxy = Proxy(url=PROXY, url_hash=uuid4().hex)
    session.add(proxy)
    await session.commit()
    proxy_id = proxy.id
    pool = ProxyPoolService(session, ProxySettings(proxy_max_bots_per_ip=1), AsyncMock())
    service = BotControlService(
        session,
        permissions=ScopedPermissions(first, second),
        proxy_pool=pool,
        tenants_use_proxy=True,
        publisher=BotCommandPublisher(build_outbox_broker(engine)),
    )
    get_me = AsyncMock(
        return_value=SimpleNamespace(id=123456, username="sample_bot", first_name="Sample")
    )
    monkeypatch.setattr(service, "_get_me", get_me)
    bot = await service.add_bot(first, TOKEN, 42)
    bot_id = bot.id
    get_me.assert_awaited_once_with(TOKEN, PROXY)
    assert bot.tenant_id == first and bot.proxy_id == proxy_id
    assert not bot.is_active and bot.status == "stopped"
    raw = await session.scalar(
        text("SELECT encrypted_token FROM bots WHERE id=:id"), {"id": bot_id}
    )
    assert raw and TOKEN not in raw and bot.token_hash == token_hash(TOKEN)
    with pytest.raises(DomainError) as error:
        await service.add_bot(second, TOKEN, 42)
    assert error.value.code == "bot_already_exists"
    with pytest.raises(DomainError) as error:
        await service.add_bot(second, NEW_TOKEN, 42)
    assert error.value.code == "no_proxy_available"
    get_me.assert_awaited_once()
    await service.replace_token(bot_id, f" {NEW_TOKEN} ", 42, tenant_id=first)
    get_me.assert_awaited_with(NEW_TOKEN, PROXY)
    assert bot.token_hash == token_hash(NEW_TOKEN)
    service.tenants_use_proxy = False
    with pytest.raises(DomainError) as error:
        await service.add_bot(second, NEW_TOKEN, 42)
    assert error.value.code == "bot_already_exists"
    await service.deactivate(bot_id, 42, tenant_id=first)
    service.tenants_use_proxy = True
    restored = await service.add_bot(second, TOKEN, 42)
    assert restored.id == bot_id and restored.tenant_id == second and restored.proxy_id == proxy_id
    assert not restored.is_deleted and not restored.is_active
    events = list(await session.scalars(select(AuditLog).where(AuditLog.entity_id == bot_id)))
    assert {event.action for event in events} == {
        "bot.added",
        "bot.deleted",
        "bot.restored",
        "bot.token_replaced",
    }
    assert TOKEN not in caplog.text and NEW_TOKEN not in caplog.text
    assert all(event.payload is None for event in events)


async def test_moderator_cannot_manage_foreign_bot_or_without_permission(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    own, foreign = await shop(session), await shop(session)
    service = BotControlService(session, permissions=ScopedPermissions(own, foreign))
    monkeypatch.setattr(
        service,
        "_get_me",
        AsyncMock(
            return_value=SimpleNamespace(id=123456, username="sample_bot", first_name="Sample")
        ),
    )
    foreign_bot = await service.add_bot(foreign, TOKEN, 42)
    bot_id = foreign_bot.id
    service.permissions = ScopedPermissions(own)
    assert await service.list_for_tenant(42, own) == []
    with pytest.raises(DomainError) as error:
        await service.list_for_tenant(42, foreign)
    assert error.value.code == "permission_denied"
    for action in (service.start, service.stop, service.restart, service.deactivate):
        with pytest.raises(DomainError) as error:
            await action(bot_id, 42, tenant_id=own)
        assert error.value.code == "bot_not_found"
    service.permissions = ScopedPermissions()
    with pytest.raises(DomainError) as error:
        await service.list_for_tenant(42, own)
    assert error.value.code == "permission_denied"
    bot = await service.repo.get(bot_id)
    assert bot is not None
    assert bot.tenant_id == foreign and not bot.is_active and bot.status == "stopped"
    assert not bot.is_deleted


async def test_invalid_identity_scope_and_lifecycle_outbox(
    session: AsyncSession, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, foreign, inactive = (
        await shop(session),
        await shop(session),
        await shop(session, active=False),
    )
    service = BotControlService(
        session,
        permissions=ScopedPermissions(tenant, inactive),
        publisher=BotCommandPublisher(build_outbox_broker(engine)),
    )
    get_me = AsyncMock(
        return_value=SimpleNamespace(id=123456, username="sample_bot", first_name="Sample")
    )
    monkeypatch.setattr(service, "_get_me", get_me)
    for target, value, code in [
        (tenant, "bad", "invalid_token_format"),
        (inactive, TOKEN, "tenant_inactive"),
        (foreign, TOKEN, "permission_denied"),
    ]:
        with pytest.raises(DomainError) as error:
            await service.add_bot(target, value, 42)
        assert error.value.code == code
    get_me.assert_not_awaited()
    assert await session.scalar(select(Bot.id)) is None
    bot_id = (await service.add_bot(tenant, TOKEN, 42)).id
    get_me.return_value.id = 999999
    with pytest.raises(DomainError) as error:
        await service.replace_token(bot_id, NEW_TOKEN, 42, tenant_id=tenant)
    assert error.value.code == "bot_identity_mismatch"
    bot = await service.repo.get(bot_id)
    assert bot is not None and bot.encrypted_token == TOKEN
    with pytest.raises(DomainError) as error:
        await service.start(bot_id, 42, tenant_id=foreign)
    assert error.value.code == "permission_denied"
    commands = (
        select(outbox_messages.c.id)
        .where(text("convert_from(payload, 'UTF8')::jsonb ->> 'bot_id' = :bot_id"))
        .params(bot_id=str(bot_id))
    )
    assert await session.scalar(commands) is None
    for action, expected in [
        (service.start, (True, "starting")),
        (service.stop, (False, "stopped")),
        (service.restart, (True, "starting")),
    ]:
        bot = await action(bot_id, 42, tenant_id=tenant)
        assert (bot.is_active, bot.status) == expected
    assert len(list(await session.scalars(commands))) == 3
    publisher = service.publisher
    monkeypatch.setattr(
        publisher, "publish_transactional", AsyncMock(side_effect=RuntimeError("not written"))
    )
    with pytest.raises(RuntimeError, match="not written"):
        await service.stop(bot_id, 42, tenant_id=tenant)
    found = await service.repo.get(bot_id)
    assert found is not None and found.is_active
    assert len(list(await session.scalars(commands))) == 3
