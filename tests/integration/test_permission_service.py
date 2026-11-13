"""PermissionService against real PostgreSQL (savepoint session) and Redis of the test stack."""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.iam.cache import GLOBAL_GENERATION_KEY, PermissionCache
from app.modules.iam.models import (
    Permission,
    Role,
    RolePermission,
    Tenant,
    TenantModerator,
    User,
    UserPermissionOverride,
    UserRole,
)
from app.modules.iam.permissions import (
    ORDERS_VIEW,
    PRODUCTS_VIEW,
    ROLE_ADMIN,
    ROLE_MODERATOR,
    ROLE_SENIOR_MODERATOR,
    TENANTS_VIEW_ALL,
)
from app.modules.iam.service import NEGATIVE_CACHE_TTL_SECONDS, PermissionService
from app.modules.iam.snapshot import DecisionSource, PermissionSnapshot
from app.modules.system.repository import SystemSettingsRepository
from app.shared_kernel.infrastructure.redis import build_redis

pytestmark = [pytest.mark.integration, pytest.mark.req("R3")]


def tg_id() -> int:
    return random.randint(10**9, 10**12)  # noqa: S311


async def make_tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(name="Shop", slug=f"shop-{uuid.uuid4().hex[:10]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def make_user(session: AsyncSession, *, superuser: bool = False) -> User:
    user = User(telegram_user_id=tg_id(), is_superuser=superuser)
    session.add(user)
    await session.flush()
    return user


async def role(session: AsyncSession, slug: str) -> Role:
    return (await session.scalars(select(Role).where(Role.slug == slug))).one()


async def permission(session: AsyncSession, slug: str) -> Permission:
    return (await session.scalars(select(Permission).where(Permission.slug == slug))).one()


async def grant_role(session: AsyncSession, user: User, slug: str) -> None:
    session.add(UserRole(user_id=user.id, role_id=(await role(session, slug)).id))
    await session.flush()


async def moderate(session: AsyncSession, user: User, tenant: Tenant) -> None:
    session.add(TenantModerator(user_id=user.id, tenant_id=tenant.id))
    await session.flush()


async def override(
    session: AsyncSession,
    user: User,
    slug: str,
    *,
    granted: bool,
    tenant: Tenant | None = None,
    expires_at: datetime | None = None,
) -> None:
    session.add(
        UserPermissionOverride(
            user_id=user.id,
            permission_id=(await permission(session, slug)).id,
            tenant_id=None if tenant is None else tenant.id,
            is_granted=granted,
            expires_at=expires_at,
        )
    )
    await session.flush()


@pytest.fixture
def service(session: AsyncSession, redis_client: Any) -> PermissionService:
    return PermissionService(session, PermissionCache(redis_client))


async def test_moderator_only_in_own_shop(
    session: AsyncSession, service: PermissionService
) -> None:
    own, foreign = await make_tenant(session), await make_tenant(session)
    user = await make_user(session)
    await grant_role(session, user, ROLE_MODERATOR)
    await moderate(session, user, own)
    tg = user.telegram_user_id
    assert await service.has_permission(tg, PRODUCTS_VIEW, own.id) is True
    assert await service.has_permission(tg, PRODUCTS_VIEW, foreign.id) is False
    assert (await service.check(tg, PRODUCTS_VIEW, foreign.id)).source is (
        DecisionSource.NO_TENANT_ACCESS
    )
    assert await service.has_permission(tg, TENANTS_VIEW_ALL) is False


@pytest.mark.parametrize("role_slug", [ROLE_SENIOR_MODERATOR, ROLE_ADMIN])
async def test_view_all_roles_work_in_any_shop(
    session: AsyncSession, service: PermissionService, role_slug: str
) -> None:
    shop = await make_tenant(session)
    user = await make_user(session)
    await grant_role(session, user, role_slug)
    decision = await service.check(user.telegram_user_id, PRODUCTS_VIEW, shop.id)
    assert decision.allowed
    assert decision.roles == frozenset({role_slug})


async def test_superuser(session: AsyncSession, service: PermissionService) -> None:
    user = await make_user(session, superuser=True)
    shop = await make_tenant(session)
    assert (await service.check(user.telegram_user_id, "anything.at_all", shop.id)).source is (
        DecisionSource.SUPERUSER
    )


async def test_tenant_override_of_global_permission_is_ignored(
    session: AsyncSession, service: PermissionService
) -> None:
    own = await make_tenant(session)
    user = await make_user(session)
    await grant_role(session, user, ROLE_SENIOR_MODERATOR)
    await moderate(session, user, own)
    await override(session, user, TENANTS_VIEW_ALL, granted=False, tenant=own)
    decision = await service.check(user.telegram_user_id, TENANTS_VIEW_ALL, own.id)
    assert decision.source is DecisionSource.ROLE  # tenant-scoped row of a global permission


async def test_overrides(session: AsyncSession, service: PermissionService) -> None:
    own, other = await make_tenant(session), await make_tenant(session)
    user = await make_user(session)
    await grant_role(session, user, ROLE_MODERATOR)
    await moderate(session, user, own)
    await moderate(session, user, other)
    await override(session, user, PRODUCTS_VIEW, granted=False)  # global deny beats role
    await override(session, user, ORDERS_VIEW, granted=False, tenant=own)  # tenant deny
    await override(session, user, "roles.manage", granted=True)  # global allow (global perm)
    await override(session, user, "payouts.approve", granted=True, tenant=own)  # tenant allow
    await session.flush()
    tg = user.telegram_user_id
    assert (await service.check(tg, PRODUCTS_VIEW, own.id)).source is (
        DecisionSource.OVERRIDE_DENY_GLOBAL
    )
    assert (await service.check(tg, ORDERS_VIEW, own.id)).source is (
        DecisionSource.OVERRIDE_DENY_TENANT
    )
    assert await service.has_permission(tg, ORDERS_VIEW, other.id) is True
    assert await service.has_permission(tg, ORDERS_VIEW) is True  # tenant deny ignored
    assert (await service.check(tg, "roles.manage")).source is (
        DecisionSource.OVERRIDE_ALLOW_GLOBAL
    )
    # ``payouts.approve`` is not in the moderator role: only the tenant override grants it
    assert (await service.check(tg, "payouts.approve", own.id)).source is (
        DecisionSource.OVERRIDE_ALLOW_TENANT
    )
    assert await service.has_permission(tg, "payouts.approve", other.id) is False
    assert await service.has_permission(tg, "payouts.approve") is False


async def test_dead_rows_are_ignored(session: AsyncSession, service: PermissionService) -> None:
    shop, dead_shop = await make_tenant(session), await make_tenant(session)
    user = await make_user(session)
    custom = Role(slug=f"r_{uuid.uuid4().hex[:8]}", name="Custom", is_active=False)
    session.add(custom)
    await session.flush()
    session.add(
        RolePermission(
            role_id=custom.id, permission_id=(await permission(session, TENANTS_VIEW_ALL)).id
        )
    )
    session.add(UserRole(user_id=user.id, role_id=custom.id))
    await grant_role(session, user, ROLE_MODERATOR)
    await moderate(session, user, shop)
    await moderate(session, user, dead_shop)
    past = datetime.now(UTC) - timedelta(minutes=1)
    await override(session, user, "roles.manage", granted=True, expires_at=past)
    await override(session, user, "users.manage", granted=True, tenant=dead_shop)
    (await permission(session, ORDERS_VIEW)).is_deleted = True
    dead_shop.is_deleted = True
    await session.flush()
    tg = user.telegram_user_id
    assert await service.has_permission(tg, TENANTS_VIEW_ALL) is False  # inactive role
    assert await service.has_permission(tg, ORDERS_VIEW, shop.id) is False  # deleted permission
    assert await service.has_permission(tg, "roles.manage") is False  # expired override
    assert (await service.check(tg, PRODUCTS_VIEW, dead_shop.id)).source is (
        DecisionSource.NO_TENANT_ACCESS
    )
    assert await service.has_permission(tg, PRODUCTS_VIEW, shop.id) is True


async def test_inactive_user(session: AsyncSession, service: PermissionService) -> None:
    user = await make_user(session)
    await grant_role(session, user, ROLE_ADMIN)
    user.is_active = False
    await session.flush()
    assert (await service.check(user.telegram_user_id, PRODUCTS_VIEW)).source is (
        DecisionSource.UNKNOWN_USER
    )


async def test_cache_aside_and_invalidation(
    session: AsyncSession, service: PermissionService, redis_client: Any
) -> None:
    user = await make_user(session)
    await grant_role(session, user, ROLE_ADMIN)
    tg = user.telegram_user_id
    key = PermissionCache.key(tg)
    assert await redis_client.exists(key) == 0
    assert await service.has_permission(tg, PRODUCTS_VIEW) is True
    iam_ttl = (await SystemSettingsRepository(session).get()).iam_cache_ttl_seconds
    assert 0 < await redis_client.ttl(key) <= iam_ttl

    await override(session, user, PRODUCTS_VIEW, granted=False)  # no invalidation yet
    assert await service.has_permission(tg, PRODUCTS_VIEW) is True  # served from cache
    assert (await service.invalidate(tg)).ok is True
    assert await service.has_permission(tg, PRODUCTS_VIEW) is False


async def test_negative_cache(service: PermissionService, redis_client: Any) -> None:
    tg = tg_id()
    assert await service.has_permission(tg, PRODUCTS_VIEW) is False
    assert 0 < await redis_client.ttl(PermissionCache.key(tg)) <= NEGATIVE_CACHE_TTL_SECONDS


async def test_ttl_follows_next_expiry(
    session: AsyncSession, service: PermissionService, redis_client: Any
) -> None:
    user = await make_user(session)
    await override(
        session,
        user,
        PRODUCTS_VIEW,
        granted=True,
        expires_at=datetime.now(UTC) + timedelta(seconds=120),
    )
    assert await service.has_permission(user.telegram_user_id, PRODUCTS_VIEW) is True
    assert 100 < await redis_client.ttl(PermissionCache.key(user.telegram_user_id)) <= 120


async def test_invalidate_all_keeps_foreign_keys(
    session: AsyncSession, service: PermissionService, redis_client: Any
) -> None:
    foreign = f"foreign:{uuid.uuid4().hex}"
    await redis_client.set(foreign, b"1", ex=60)
    try:
        targets = [await make_user(session) for _ in range(3)]
        for user in targets:
            await grant_role(session, user, ROLE_MODERATOR)
            assert await service.has_permission(user.telegram_user_id, PRODUCTS_VIEW) is True
            assert await redis_client.exists(PermissionCache.key(user.telegram_user_id)) == 1
        assert (await service.invalidate_all()).ok is True
        for user in targets:  # global generation bumped -> every stamped snapshot is stale
            assert await PermissionCache(redis_client).get(user.telegram_user_id) is None
        assert await redis_client.get(foreign) == b"1"
    finally:
        await redis_client.delete(foreign)


async def test_guarded_write_lua(redis_client: Any) -> None:
    """Real Lua script: written only while both generations stayed unchanged."""
    cache = PermissionCache(redis_client)
    tg = tg_id()
    await redis_client.set(GLOBAL_GENERATION_KEY, 5)
    snapshot = PermissionSnapshot(user_id=uuid4())
    assert await cache.set(snapshot, tg, 60, generations=(0, 5)) is True
    stored = PermissionSnapshot.from_json(await redis_client.get(cache.key(tg)))
    assert stored is not None and stored.global_generation == 5

    bumped = PermissionSnapshot(user_id=uuid4())
    await redis_client.incr(GLOBAL_GENERATION_KEY)  # invalidation between read and write
    assert await cache.set(bumped, tg, 60, generations=(0, 5)) is False
    assert await cache.set(snapshot, tg, 60, generations=(0, 6)) is True
    assert await cache.set(bumped, tg, 60, generations=(1, 6)) is False  # user gen differs


@pytest.mark.req("S5")
async def test_redis_down_falls_back_to_postgres(session: AsyncSession, settings: Settings) -> None:
    broken = settings.model_copy(update={"redis": settings.redis.model_copy(update={"port": 1})})
    client = build_redis(broken)
    try:
        service = PermissionService(session, PermissionCache(client))
        shop = await make_tenant(session)
        user = await make_user(session)
        await grant_role(session, user, ROLE_MODERATOR)
        await moderate(session, user, shop)
        assert await service.has_permission(user.telegram_user_id, PRODUCTS_VIEW, shop.id) is True
        assert (
            await service.has_permission(user.telegram_user_id, PRODUCTS_VIEW, uuid.uuid4())
            is False
        )
        assert (await service.invalidate(user.telegram_user_id)).ok is False
        assert (await service.invalidate_all()).ok is False
    finally:
        await client.aclose()
