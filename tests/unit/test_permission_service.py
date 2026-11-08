"""Unit tests of :class:`PermissionService` (mocked cache and repositories)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from dishka import Provider, Scope, make_async_container, provide
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.iam.cache import InvalidationResult, PermissionCache
from app.modules.iam.ioc import IamProvider
from app.modules.iam.permissions import PRODUCTS_EDIT, PRODUCTS_VIEW
from app.modules.iam.service import (
    INVALID_PERMISSION_SLUG,
    NEGATIVE_CACHE_TTL_SECONDS,
    PERMISSION_DENIED,
    PermissionService,
)
from app.modules.iam.snapshot import DecisionSource, PermissionSnapshot
from app.modules.system.models import DEFAULT_IAM_CACHE_TTL_SECONDS
from app.shared_kernel.domain.exceptions import (
    EntityNotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.shared_kernel.infrastructure.providers import RedisProvider

NOW = datetime(2030, 1, 1, tzinfo=UTC)
TG = 1001


def user_snapshot(**kwargs: Any) -> PermissionSnapshot:
    return PermissionSnapshot(
        user_id=uuid4(),
        role_permissions={PRODUCTS_VIEW: frozenset({"moderator"})},
        **kwargs,
    )


def make_service(
    *,
    cached: PermissionSnapshot | None = None,
    loaded: PermissionSnapshot | None = None,
    ttl: int | None = 3600,
) -> tuple[PermissionService, MagicMock, MagicMock]:
    cache = MagicMock()
    cache.get_with_generations = AsyncMock(return_value=None if cached is None else (cached, 0, 0))
    cache.read_generations = AsyncMock(return_value=(0, 0))
    cache.set = AsyncMock(return_value=True)
    cache.invalidate = AsyncMock(return_value=InvalidationResult(removed=1))
    cache.invalidate_all = AsyncMock(return_value=InvalidationResult())
    queries = MagicMock()
    queries.load_snapshot = AsyncMock(return_value=loaded)
    system = MagicMock()
    if ttl is None:
        system.get = AsyncMock(side_effect=EntityNotFoundError("x", code="system_settings_missing"))
    else:
        system.get = AsyncMock(return_value=SimpleNamespace(iam_cache_ttl_seconds=ttl))
    session = MagicMock()
    session.configure_mock(new=[], dirty=[], deleted=[])
    service = PermissionService(
        session, cache, queries=queries, system_settings=system, clock=lambda: NOW
    )
    return service, cache, queries


async def test_hit_does_not_touch_database() -> None:
    service, cache, queries = make_service(cached=user_snapshot())
    assert await service.has_permission(TG, PRODUCTS_VIEW) is True
    queries.load_snapshot.assert_not_awaited()
    cache.set.assert_not_awaited()


async def test_miss_loads_and_caches_with_iam_ttl() -> None:
    snapshot = user_snapshot()
    service, cache, queries = make_service(loaded=snapshot, ttl=3600)
    assert await service.get_snapshot(TG) is snapshot
    queries.load_snapshot.assert_awaited_once_with(TG, now=NOW)
    cache.set.assert_awaited_once_with(snapshot, TG, 3600, generations=(0, 0))


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=90.2), 91),
        (timedelta(days=5), 3600),
        (timedelta(seconds=0.1), 1),
        (timedelta(seconds=-5), 1),
    ],
)
async def test_ttl_is_capped_by_next_expiry(delta: timedelta, expected: int) -> None:
    snapshot = user_snapshot(next_expiry=NOW + delta)
    service, cache, _ = make_service(loaded=snapshot, ttl=3600)
    await service.get_snapshot(TG)
    cache.set.assert_awaited_once_with(snapshot, TG, expected, generations=(0, 0))


async def test_negative_cache_ttl() -> None:
    service, cache, _ = make_service(loaded=PermissionSnapshot.missing())
    assert await service.has_permission(TG, PRODUCTS_VIEW) is False
    cache.set.assert_awaited_once_with(
        PermissionSnapshot.missing(), TG, NEGATIVE_CACHE_TTL_SECONDS, generations=(0, 0)
    )
    assert NEGATIVE_CACHE_TTL_SECONDS == 60


async def test_fallback_ttl_without_system_settings() -> None:
    snapshot = user_snapshot()
    service, cache, _ = make_service(loaded=snapshot, ttl=None)
    await service.get_snapshot(TG)
    cache.set.assert_awaited_once_with(
        snapshot, TG, DEFAULT_IAM_CACHE_TTL_SECONDS, generations=(0, 0)
    )


async def test_check_and_require() -> None:
    service, _, _ = make_service(cached=user_snapshot())
    decision = await service.require(TG, PRODUCTS_VIEW)
    assert decision.source is DecisionSource.ROLE
    assert (await service.check(TG, PRODUCTS_EDIT)).source is DecisionSource.NOT_GRANTED
    with pytest.raises(PermissionDeniedError) as info:
        await service.require(TG, PRODUCTS_EDIT, uuid4())
    assert info.value.code == PERMISSION_DENIED


@pytest.mark.parametrize("slug", ["", "products", "Products.view", "products.view.x", "a.b-c"])
async def test_malformed_slug(slug: str) -> None:
    service, cache, _ = make_service(cached=user_snapshot())
    with pytest.raises(ValidationError) as info:
        await service.has_permission(TG, slug)
    assert info.value.code == INVALID_PERMISSION_SLUG
    cache.get_with_generations.assert_not_awaited()


async def test_unknown_but_well_formed_slug_is_denied() -> None:
    service, _, _ = make_service(cached=user_snapshot())
    assert await service.has_permission(TG, "future.thing") is False


async def test_invalidation_delegates() -> None:
    service, cache, _ = make_service()
    assert (await service.invalidate(TG)).ok is True
    cache.invalidate.assert_awaited_once_with(TG)
    assert (await service.invalidate_all()).ok is True
    cache.invalidate_all.assert_awaited_once_with()


async def test_unconfirmed_invalidation_is_reported() -> None:
    service, cache, _ = make_service()
    cache.invalidate = AsyncMock(return_value=InvalidationResult(ok=False))
    assert (await service.invalidate(TG)).ok is False


async def test_redis_down_does_not_write() -> None:
    snapshot = user_snapshot()
    service, cache, _ = make_service(loaded=snapshot)
    cache.read_generations = AsyncMock(return_value=None)
    assert await service.get_snapshot(TG) is snapshot
    cache.set.assert_not_awaited()


async def test_uninspectable_session_is_not_cached() -> None:
    snapshot = user_snapshot()
    service, cache, _ = make_service(loaded=snapshot)
    for name in ("new", "dirty", "deleted"):
        setattr(service._session, name, None)
        service._snapshots.clear()
        cache.set.reset_mock()
        assert await service.get_snapshot(TG) is snapshot
        cache.set.assert_not_awaited()


async def test_dirty_session_is_not_cached() -> None:
    snapshot = user_snapshot()
    for attr in ("new", "dirty", "deleted"):
        service, cache, _ = make_service(loaded=snapshot)
        setattr(service._session, attr, [object()])
        assert await service.get_snapshot(TG) is snapshot
        cache.set.assert_not_awaited()


async def test_snapshot_memoised_per_request() -> None:
    snapshot = user_snapshot()
    service, cache, queries = make_service(loaded=snapshot)
    await service.has_permission(TG, PRODUCTS_VIEW)
    await service.has_permission(TG, PRODUCTS_EDIT)
    await service.check(TG, PRODUCTS_VIEW, uuid4())
    assert queries.load_snapshot.await_count == 1
    assert cache.get_with_generations.await_count == 1


async def test_memoised_snapshot_dropped_on_invalidate() -> None:
    snapshot = user_snapshot()
    service, cache, queries = make_service(loaded=snapshot)
    await service.has_permission(TG, PRODUCTS_VIEW)
    await service.invalidate(TG)
    await service.has_permission(TG, PRODUCTS_VIEW)
    assert queries.load_snapshot.await_count == 2


class _FakeSessionProvider(Provider):
    @provide(scope=Scope.REQUEST)
    def session(self) -> AsyncSession:
        return MagicMock(spec=AsyncSession)


async def test_container_wiring() -> None:
    settings = Settings(_env_file=None)
    container = make_async_container(
        RedisProvider(), IamProvider(), _FakeSessionProvider(), context={Settings: settings}
    )
    try:
        redis = await container.get(Redis)
        cache = await container.get(PermissionCache)
        assert cache is await container.get(PermissionCache)
        assert "password" not in repr(redis).lower()
        async with container() as request:
            service = await request.get(PermissionService)
            assert isinstance(service, PermissionService)
    finally:
        await container.close()
