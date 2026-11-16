"""ProxyPoolService / ProxyHealthTracker against the test PostgreSQL and Redis (DB 15).

The service commits itself: service tests use ``clean_db`` (real commits, truncated after
the test) and fresh sessions per call (separate connections, ``NullPool``); data is passed
between steps as ids.
"""

# ruff: noqa: S608

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from alembic import command
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import ProxySettings
from app.modules.bot_manager.metrics import FAILURE_VERDICTS, LabeledCounter
from app.modules.bot_manager.models import Bot, Proxy
from app.modules.bot_manager.proxy_health import ProxyHealthTracker, strike_key
from app.modules.bot_manager.proxy_repository import ProxyRepository
from app.modules.bot_manager.proxy_service import (
    NoProxyAvailableError,
    ProxyPoolService,
    proxy_url_hash,
)
from app.modules.bot_manager.security import token_hash
from app.modules.iam.permissions import SEED_PERMISSION_SLUGS
from app.modules.iam.superuser import SuperuserInstaller
from app.shared_kernel.domain.exceptions import EntityAlreadyExistsError, PermissionDeniedError
from tests.integration.conftest import Query
from tests.integration.conftest import run_alembic as _alembic
from tests.integration.test_iam_models import _tenant

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.14")]

ACTOR = 777_000


class _Allow:
    def __init__(self) -> None:
        self.slugs: list[str] = []

    async def require(self, telegram_user_id: int, slug: str, tenant_id: Any = None) -> None:
        self.slugs.append(slug)


class _Deny:
    async def require(self, telegram_user_id: int, slug: str, tenant_id: Any = None) -> None:
        raise PermissionDeniedError("denied", code="permission_denied")


class _DownRedis:
    async def set(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisConnectionError("down")

    async def mget(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisConnectionError("down")

    async def delete(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisConnectionError("down")


def _settings(**overrides: Any) -> ProxySettings:
    values: dict[str, Any] = {
        "proxy_max_bots_per_ip": 2,
        "proxy_strikes_to_quarantine": 2,
        "proxy_quarantine_days": 7,
        "proxy_strike_window_seconds": 3600,
        "proxy_failure_threshold": 5,
        "proxy_outage_ratio": 0.5,
    }
    values.update(overrides)
    return ProxySettings.model_validate(values)


@pytest.fixture
async def redis(redis_client: Any) -> AsyncIterator[Any]:
    async def drop() -> None:
        keys = [key async for key in redis_client.scan_iter(match="proxy:*", count=500)]
        if keys:
            await redis_client.delete(*keys)

    await drop()
    yield redis_client
    await drop()


@pytest.fixture
def factory(clean_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(clean_db, expire_on_commit=False)


class Pool:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], redis: Any, settings: ProxySettings
    ) -> None:
        self.factory = factory
        self.settings = settings
        self.verdicts = LabeledCounter(FAILURE_VERDICTS)
        self.tracker = ProxyHealthTracker(
            redis, window_seconds=settings.proxy_strike_window_seconds, ok_throttle_seconds=0
        )

    async def call(self, method: str, *args: Any, tracker: Any = None, perms: Any = None) -> Any:
        async with self.factory() as session:
            service = ProxyPoolService(
                session,
                self.settings,
                tracker or self.tracker,
                permissions=perms,
                verdicts=self.verdicts,
            )
            return await getattr(service, method)(*args)

    async def sql(self, statement: str, **params: Any) -> Any:
        async with self.factory() as session:
            result: Any = await session.execute(text(statement), params)
            rows = result.all() if result.returns_rows else None
            await session.commit()
            return rows


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    proxies: int,
    bots: int,
    strikes: list[int] | None = None,
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    async with factory() as session:
        tenant = await _tenant(session)
        proxy_rows = []
        for index in range(proxies):
            url = f"socks5://user:pass@10.9.0.{index + 1}:1080"
            proxy_rows.append(
                Proxy(
                    url=url,
                    url_hash=proxy_url_hash(url),
                    strike_count=(strikes or [0] * proxies)[index],
                )
            )
        bot_rows = [
            Bot(
                tenant_id=tenant.id,
                telegram_bot_id=880_000 + index,
                encrypted_token=f"{index}:tok",
                token_hash=token_hash(f"{index}:tok"),
            )
            for index in range(bots)
        ]
        session.add_all([*proxy_rows, *bot_rows])
        await session.flush()
        ids = [p.id for p in proxy_rows], [b.id for b in bot_rows]
        await session.commit()
    return ids


def _slow_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen the window between the proxy lock and the bind: without ``FOR UPDATE`` every
    parallel assignment would see the same count and overfill the proxy."""
    original = ProxyRepository.bot_count

    async def slow(self: ProxyRepository, proxy_id: uuid.UUID) -> int:
        await asyncio.sleep(0.05)
        return await original(self, proxy_id)

    monkeypatch.setattr(ProxyRepository, "bot_count", slow)


async def test_parallel_assignment_respects_limit(
    factory: Any, redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slow_count(monkeypatch)
    pool = Pool(factory, redis, _settings(proxy_max_bots_per_ip=3))
    (proxy,), bots = await _seed(factory, 1, 10)
    results = await asyncio.gather(
        *(pool.call("assign_proxy", bot) for bot in bots), return_exceptions=True
    )
    kinds = sorted(type(r).__name__ for r in results)
    assert kinds == ["NoProxyAvailableError"] * 7 + ["UUID"] * 3
    rows = await pool.sql("SELECT count(*) FROM bots WHERE proxy_id = :p", p=proxy)
    assert rows[0][0] == 3


async def test_parallel_assignment_and_ban_do_not_deadlock(
    factory: Any, redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slow_count(monkeypatch)
    pool = Pool(factory, redis, _settings(proxy_max_bots_per_ip=10, proxy_strikes_to_quarantine=1))
    (a, b), bots = await _seed(factory, 2, 6)
    calls = [pool.call("assign_proxy", bot) for bot in bots]
    calls.insert(2, pool.call("ban", a, ACTOR, perms=_Allow()))
    calls.insert(4, pool.call("register_strike", b, "TimeoutError"))
    results = await asyncio.wait_for(asyncio.gather(*calls, return_exceptions=True), 60)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert all(isinstance(e, NoProxyAvailableError) for e in errors), errors
    rows = await pool.sql("SELECT count(*) FROM bots WHERE proxy_id IS NOT NULL")
    assert rows[0][0] == 0  # assigned before the ban/quarantine -> released, after -> none
    rows = await pool.sql("SELECT status FROM proxies ORDER BY status")
    assert [r[0] for r in rows] == ["banned", "quarantine"]


async def test_ordering_limit_sticky_release(factory: Any, redis: Any) -> None:
    pool = Pool(factory, redis, _settings(proxy_max_bots_per_ip=2))
    (a, b), bots = await _seed(factory, 2, 5, strikes=[1, 0])
    # fewer strikes first, then load
    assert await pool.call("assign_proxy", bots[0]) == b
    assert await pool.call("assign_proxy", bots[0]) == b  # sticky
    assert await pool.call("assign_proxy", bots[1]) == b
    assert await pool.call("assign_proxy", bots[2]) == a  # b is full
    assert await pool.call("assign_proxy", bots[3]) == a
    with pytest.raises(NoProxyAvailableError):
        await pool.call("assign_proxy", bots[4])
    assert await pool.call("get_available_proxy") is None
    assert await pool.call("release_proxy", bots[3]) == a
    available = await pool.call("get_available_proxy")
    assert available is not None and available.id == a


async def test_sticky_proxy_left_when_quarantined(factory: Any, redis: Any) -> None:
    pool = Pool(factory, redis, _settings(proxy_max_bots_per_ip=5))
    (a, b), (bot,) = await _seed(factory, 2, 1)
    first = await pool.call("assign_proxy", bot)
    await pool.sql("UPDATE proxies SET status = 'quarantine' WHERE id = :p", p=first)
    assert await pool.call("assign_proxy", bot) == ({a, b} - {first}).pop()


async def test_strike_dedup_quarantine_and_rehabilitate(factory: Any, redis: Any) -> None:
    pool = Pool(factory, redis, _settings(proxy_max_bots_per_ip=5))
    (a,), bots = await _seed(factory, 1, 2)  # both bots land on ``a``
    for bot in bots:
        await pool.call("assign_proxy", bot)
    first = await pool.call("register_strike", a, "TelegramNetworkError")
    assert first.applied and not first.quarantined
    second = await pool.call("register_strike", a, "TelegramNetworkError")
    assert not second.applied  # same window
    await redis.delete(strike_key(a))  # next window
    third = await pool.call("register_strike", a, "TelegramNetworkError")
    assert third.quarantined
    assert set(third.released_bot_ids) == set(bots)
    rows = await pool.sql(
        "SELECT status, strike_count, last_ban_at FROM proxies WHERE id = :p", p=a
    )
    assert rows[0][0] == "quarantine" and rows[0][1] == 2 and rows[0][2] is not None
    rows = await pool.sql("SELECT count(*) FROM bots WHERE proxy_id IS NOT NULL")
    assert rows[0][0] == 0
    audit = await pool.sql(
        "SELECT action, actor_type FROM audit_logs WHERE entity_id = :p ORDER BY created_at", p=a
    )
    assert [tuple(r) for r in audit] == [
        ("proxy.strike", "system"),
        ("proxy.quarantine", "system"),
    ]
    now = datetime.now(UTC)
    assert await pool.call("rehabilitate", now) == 0  # too early
    later = now + timedelta(days=8)
    assert await pool.call("rehabilitate", later) == 1
    assert await pool.call("rehabilitate", later) == 0  # idempotent
    rows = await pool.sql("SELECT status, strike_count FROM proxies WHERE id = :p", p=a)
    assert tuple(rows[0]) == ("active", 0)


async def test_strike_dedup_falls_back_to_last_strike_at(factory: Any, redis: Any) -> None:
    pool = Pool(factory, redis, _settings())
    (a, _b), _ = await _seed(factory, 2, 0)
    down = ProxyHealthTracker(_DownRedis(), window_seconds=3600)
    assert (await pool.call("register_strike", a, "x", tracker=down)).applied
    assert not (await pool.call("register_strike", a, "x", tracker=down)).applied


async def test_on_delete_set_null(factory: Any, redis: Any) -> None:
    pool = Pool(factory, redis, _settings())
    (a,), (bot,) = await _seed(factory, 1, 1)
    await pool.call("assign_proxy", bot)
    await pool.sql("DELETE FROM proxies WHERE id = :p", p=a)
    rows = await pool.sql("SELECT proxy_id FROM bots WHERE id = :b", b=bot)
    assert rows[0][0] is None


async def test_correlation_strike_when_others_ok(factory: Any, redis: Any) -> None:
    pool = Pool(factory, redis, _settings())
    (a, b), _ = await _seed(factory, 2, 0)
    await pool.tracker.mark_ok(b)
    await pool.tracker.mark_fail(a)
    verdict = await pool.call("report_failure", a, TimeoutError())
    assert verdict.verdict == "strike"
    rows = await pool.sql("SELECT strike_count, last_error FROM proxies WHERE id = :p", p=a)
    assert tuple(rows[0]) == (1, "TimeoutError")
    assert (await pool.call("report_failure", a, TimeoutError())).verdict == "deduplicated"
    assert pool.verdicts.snapshot()["strike"] == 1


async def test_correlation_outage_when_all_fail(factory: Any, redis: Any) -> None:
    pool = Pool(factory, redis, _settings())
    (a, b), _ = await _seed(factory, 2, 0)
    await pool.tracker.mark_ok(b)
    await pool.tracker.mark_fail(b)  # newer fail wins
    await pool.tracker.mark_fail(a)
    assert (await pool.call("report_failure", a, TimeoutError())).verdict == "outage"
    assert await pool.tracker.outage_active()
    assert 0 < await redis.ttl("proxy:outage") <= 3600
    rows = await pool.sql("SELECT sum(strike_count) FROM proxies")
    assert rows[0][0] == 0
    # another process (own tracker, no local state) clears the shared key
    other = ProxyHealthTracker(redis, window_seconds=3600)
    assert await other.mark_ok(b)
    assert not await pool.tracker.outage_active()


async def test_correlation_outage_without_ok_marks(factory: Any, redis: Any) -> None:
    pool = Pool(factory, redis, _settings())
    (a, _b, _c), _ = await _seed(factory, 3, 0)
    assert (await pool.call("report_failure", a, TimeoutError())).verdict == "outage"


async def test_correlation_single_proxy(factory: Any, redis: Any) -> None:
    pool = Pool(factory, redis, _settings())
    (a,), _ = await _seed(factory, 1, 0)
    assert (await pool.call("report_failure", a, TimeoutError())).verdict == "single_proxy"
    rows = await pool.sql("SELECT strike_count FROM proxies")
    assert rows[0][0] == 0


async def test_correlation_redis_down_is_fail_closed(factory: Any, redis: Any) -> None:
    pool = Pool(factory, redis, _settings())
    (a, b), _ = await _seed(factory, 2, 0)
    await pool.tracker.mark_ok(b)
    down = ProxyHealthTracker(_DownRedis(), window_seconds=3600)
    verdict = await pool.call("report_failure", a, TimeoutError(), tracker=down)
    assert verdict.verdict == "error"
    assert pool.verdicts.snapshot()["error"] == 1
    rows = await pool.sql("SELECT sum(strike_count) FROM proxies")
    assert rows[0][0] == 0


async def test_report_failure_strike_without_redis_fallback(factory: Any, redis: Any) -> None:
    """Health readable, strike key not: no PG fallback inside ``report_failure``."""
    pool = Pool(factory, redis, _settings())
    (a, b), _ = await _seed(factory, 2, 0)
    await pool.tracker.mark_ok(b)

    class _NoStrikeKey(ProxyHealthTracker):
        async def try_acquire_strike(self, proxy_id: uuid.UUID) -> str | None:
            raise RedisConnectionError("down")

    tracker = _NoStrikeKey(redis, window_seconds=3600)
    assert (await pool.call("report_failure", a, None, tracker=tracker)).verdict == "error"
    rows = await pool.sql("SELECT strike_count FROM proxies WHERE id = :p", p=a)
    assert rows[0][0] == 0


async def test_failed_strike_frees_dedup_key(
    factory: Any, redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = Pool(factory, redis, _settings())
    (a, _b), _ = await _seed(factory, 2, 0)

    async def boom(self: ProxyRepository, proxy_id: uuid.UUID, *, for_update: bool = False) -> Any:
        raise RuntimeError("db failure")

    monkeypatch.setattr(ProxyRepository, "get", boom)
    with pytest.raises(RuntimeError):
        await pool.call("register_strike", a, "x")
    assert await redis.exists(strike_key(a)) == 0


async def test_manual_add_ban_activate(factory: Any, redis: Any) -> None:
    pool = Pool(factory, redis, _settings())
    perms = _Allow()
    _, (bot,) = await _seed(factory, 0, 1)
    proxy = await pool.call(
        "add", "SOCKS5://u:secret@Proxy.Example:1080", " main ", ACTOR, perms=perms
    )
    with pytest.raises(EntityAlreadyExistsError):  # same proxy, other case of scheme/host
        await pool.call("add", "socks5://u:secret@proxy.example:1080", None, ACTOR, perms=perms)
    with pytest.raises(PermissionDeniedError):
        await pool.call("ban", proxy, ACTOR, perms=_Deny())
    assert await pool.call("assign_proxy", bot) == proxy
    assert await pool.call("ban", proxy, ACTOR, perms=perms) == (bot,)
    await pool.call("activate", proxy, ACTOR, perms=perms)
    assert set(perms.slugs) == {"proxies.manage"}
    rows = await pool.sql("SELECT status, label, url FROM proxies WHERE id = :p", p=proxy)
    assert rows[0][0] == "active" and rows[0][1] == "main"
    assert "secret" not in rows[0][2]  # encrypted at rest
    async with factory() as session:
        stored = await session.get(Proxy, proxy)
        assert stored is not None and stored.url == "socks5://u:secret@proxy.example:1080"
    audit = await pool.sql(
        "SELECT action, actor_id, payload::text FROM audit_logs WHERE entity_id = :p "
        "ORDER BY created_at",
        p=proxy,
    )
    assert [r[0] for r in audit] == ["proxy.add", "proxy.ban", "proxy.activate"]
    assert all(r[1] == ACTOR and "secret" not in r[2] for r in audit)


async def test_release_strike_only_by_owner(redis: Any) -> None:
    tracker = ProxyHealthTracker(redis, window_seconds=3600)
    proxy_id = uuid.uuid4()
    token = await tracker.try_acquire_strike(proxy_id)
    assert token is not None
    assert await tracker.try_acquire_strike(proxy_id) is None  # taken
    assert not await tracker.release_strike(proxy_id, "foreign-token")
    assert await redis.exists(strike_key(proxy_id)) == 1
    assert await tracker.release_strike(proxy_id, token)
    assert await redis.exists(strike_key(proxy_id)) == 0
    # expired and re-taken by another process: the old owner can not remove it
    other = await tracker.try_acquire_strike(proxy_id)
    assert other is not None and other != token
    assert not await tracker.release_strike(proxy_id, token)
    assert await redis.exists(strike_key(proxy_id)) == 1


# --- head catalogue and the revision ring -------------------------------------------------

_ADMIN_SLUGS = (
    "SELECT p.slug FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
    "JOIN permissions p ON p.id = rp.permission_id WHERE r.slug = 'admin'"
)


async def test_head_permission_catalogue_matches_seed(session: AsyncSession) -> None:
    slugs = set((await session.scalars(text("SELECT slug FROM permissions"))).all())
    assert slugs == SEED_PERMISSION_SLUGS
    assert len(slugs) == 32
    assert set((await session.scalars(text(_ADMIN_SLUGS))).all()) == SEED_PERMISSION_SLUGS
    # The superuser installer refuses to run on an incomplete catalogue: head is complete.
    await SuperuserInstaller(session, cast(Any, None))._ensure_seed()


def test_proxy_pool_revision_ring(migrated_scratch_db: tuple[str, Query]) -> None:
    url, query = migrated_scratch_db
    _alembic(url, command.downgrade, "bots")
    assert query("SELECT to_regclass('public.proxies')") == [(None,)]
    assert (
        query(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'bots' AND column_name = 'proxy_id'"
        )
        == []
    )
    assert query("SELECT count(*) FROM permissions WHERE slug = 'proxies.manage'") == [(0,)]
    assert len(query(_ADMIN_SLUGS)) == 28
    _alembic(url, command.upgrade, "head")
    assert query("SELECT to_regclass('public.proxies')") != [(None,)]
    assert query("SELECT indexname FROM pg_indexes WHERE indexname = 'ix_bots_proxy_id'") == [
        ("ix_bots_proxy_id",)
    ]
    assert {r[0] for r in query(_ADMIN_SLUGS)} == SEED_PERMISSION_SLUGS


_ROLE_LINKS = (
    "SELECT r.slug, p.slug FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
    "JOIN permissions p ON p.id = rp.permission_id WHERE r.slug IN ({roles}) "
    "AND p.slug IN ('users.manage', 'logs.view', 'moderators.manage', 'tenants.manage', "
    "'bots.manage') ORDER BY 1, 2"
)
_ROLE_COUNTS = (
    "SELECT r.slug, count(*) FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
    "WHERE r.slug IN ('admin', 'senior_moderator', 'moderator') GROUP BY 1 ORDER BY 1"
)
_CUSTOMER_NAMES = (
    "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass('public.{t}') "
    "UNION SELECT indexname FROM pg_indexes WHERE tablename = '{t}' ORDER BY 1"
)


def _customer_names(table: str) -> list[str]:
    return sorted(
        name.format(t=table)
        for name in (
            "pk_{t}",
            "fk_{t}_tenant_id_tenants",
            "fk_{t}_language_code_languages",
            "fk_{t}_first_bot_id_bots",
            "uq_{t}_tenant_id_telegram_user_id",
            "ix_{t}_first_bot_id",
            "ix_{t}_language_code",
        )
    )


@pytest.mark.req("R-II.2", "R-II.4")
def test_staff_permissions_revision_ring(migrated_scratch_db: tuple[str, Query]) -> None:
    """``staff_permissions``: logs.view for admin, users.manage only for admin, customers."""
    url, query = migrated_scratch_db
    roles = "'admin', 'senior_moderator', 'moderator'"

    def check_head() -> None:
        assert [tuple(r) for r in query(_ROLE_LINKS.format(roles=roles))] == [
            ("admin", "bots.manage"),
            ("admin", "logs.view"),
            ("admin", "moderators.manage"),
            ("admin", "tenants.manage"),
            ("admin", "users.manage"),
            ("moderator", "bots.manage"),
            ("senior_moderator", "bots.manage"),
        ]
        assert [tuple(r) for r in query(_ROLE_COUNTS)] == [
            ("admin", 32),
            ("moderator", 20),
            ("senior_moderator", 21),
        ]
        assert {r[0] for r in query(_ADMIN_SLUGS)} == SEED_PERMISSION_SLUGS
        assert query("SELECT to_regclass('public.tenant_customers')") == [(None,)]
        assert [r[0] for r in query(_CUSTOMER_NAMES.format(t="customers"))] == _customer_names(
            "customers"
        )

    check_head()
    _alembic(url, command.downgrade, "proxy_pool")
    assert query(
        "SELECT count(*) FROM permissions WHERE slug IN ('logs.view', 'tenants.manage', "
        "'bots.manage')"
    ) == [(0,)]
    assert [tuple(r) for r in query(_ROLE_LINKS.format(roles=roles))] == [
        ("admin", "moderators.manage"),
        ("admin", "users.manage"),
        ("moderator", "moderators.manage"),
        ("moderator", "users.manage"),
        ("senior_moderator", "moderators.manage"),
        ("senior_moderator", "users.manage"),
    ]
    assert [tuple(r) for r in query(_ROLE_COUNTS)] == [
        ("admin", 29),
        ("moderator", 21),
        ("senior_moderator", 22),
    ]
    assert query("SELECT to_regclass('public.customers')") == [(None,)]
    assert [r[0] for r in query(_CUSTOMER_NAMES.format(t="tenant_customers"))] == (
        _customer_names("tenant_customers")
    )
    _alembic(url, command.upgrade, "head")
    check_head()


@pytest.mark.req("R-II.2")
def test_staff_permissions_guard_blocks_tenant_overrides(
    migrated_scratch_db: tuple[str, Query],
) -> None:
    """users.view/users.manage become global: tenant-scoped overrides stop the upgrade."""
    url, query = migrated_scratch_db
    _alembic(url, command.downgrade, "proxy_pool")
    query(
        "INSERT INTO tenants (id, slug, name) "
        "VALUES ('00000000-0000-0000-0000-0000000000a1', 'guard-shop', 'Guard')"
    )
    query(
        "INSERT INTO users (id, telegram_user_id) "
        "VALUES ('00000000-0000-0000-0000-0000000000b1', 990000001)"
    )
    query(
        "INSERT INTO user_permission_overrides (id, user_id, permission_id, tenant_id, is_granted) "
        "SELECT gen_random_uuid(), '00000000-0000-0000-0000-0000000000b1', p.id, "
        "'00000000-0000-0000-0000-0000000000a1', true FROM permissions p "
        "WHERE p.slug = 'users.manage'"
    )
    with pytest.raises(DBAPIError, match="tenant-scoped overrides of users.view/users.manage"):
        _alembic(url, command.upgrade, "head")
    assert query("SELECT count(*) FROM permissions WHERE slug = 'logs.view'") == [(0,)]
    assert query("SELECT to_regclass('public.tenant_customers')") != [(None,)]
    query("UPDATE user_permission_overrides SET tenant_id = NULL")  # make it global
    _alembic(url, command.upgrade, "head")
    assert query("SELECT count(*) FROM permissions WHERE slug = 'logs.view'") == [(1,)]


@pytest.mark.req("R-II.2")
def test_staff_permissions_drops_global_access_assignments(
    migrated_scratch_db: tuple[str, Query],
) -> None:
    """Superusers / holders of tenants.view_all lose shop assignments, each audited."""
    url, query = migrated_scratch_db
    _alembic(url, command.downgrade, "proxy_pool")
    shop = "00000000-0000-0000-0000-0000000000a2"
    query(f"INSERT INTO tenants (id, slug, name) VALUES ('{shop}', 'ga-shop', 'GA')")
    users = {
        "super": ("00000000-0000-0000-0000-0000000000c1", 990000011, True, None),
        "senior": ("00000000-0000-0000-0000-0000000000c2", 990000012, False, "senior_moderator"),
        "mod": ("00000000-0000-0000-0000-0000000000c3", 990000013, False, "moderator"),
    }
    for uid, tg, superuser, role in users.values():
        query(
            "INSERT INTO users (id, telegram_user_id, is_superuser) "
            f"VALUES ('{uid}', {tg}, {str(superuser).lower()})"
        )
        if role:
            query(
                "INSERT INTO user_roles (id, user_id, role_id) "
                f"SELECT gen_random_uuid(), '{uid}', id FROM roles WHERE slug = '{role}'"
            )
        query(
            "INSERT INTO tenant_moderators (id, user_id, tenant_id) "
            f"VALUES (gen_random_uuid(), '{uid}', '{shop}')"
        )
    _alembic(url, command.upgrade, "head")
    assert query("SELECT user_id::text FROM tenant_moderators") == [(users["mod"][0],)]
    audits = query(
        "SELECT entity_id::text, actor_type, payload->>'reason', tenant_id::text "
        "FROM audit_logs WHERE action = 'iam.moderator.unassign' ORDER BY 1"
    )
    assert [tuple(r) for r in audits] == [
        (users["super"][0], "system", "global_access", shop),
        (users["senior"][0], "system", "global_access", shop),
    ]
