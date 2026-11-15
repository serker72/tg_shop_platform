"""ProxyPoolService / ProxyHealthTracker against the test PostgreSQL and Redis (DB 15).

The service commits itself: every test uses ``clean_db`` (real commits, truncated after the
test) and fresh sessions; data is passed between steps as ids.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import ProxySettings
from app.modules.bot_manager.metrics import FAILURE_VERDICTS, LabeledCounter, OutageGauge
from app.modules.bot_manager.models import Bot, Proxy
from app.modules.bot_manager.proxy_health import ProxyHealthTracker, strike_key
from app.modules.bot_manager.proxy_service import (
    NoProxyAvailableError,
    ProxyPoolService,
    proxy_url_hash,
)
from app.modules.bot_manager.security import token_hash
from app.shared_kernel.domain.exceptions import EntityAlreadyExistsError, PermissionDeniedError
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


def _settings(**overrides: Any) -> ProxySettings:
    values: dict[str, Any] = {
        "master_bot_use_proxy": False,
        "master_bot_proxy_url": None,
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
        self.outage = OutageGauge()
        self.tracker = ProxyHealthTracker(
            redis,
            window_seconds=settings.proxy_strike_window_seconds,
            ok_throttle_seconds=0,
            outage=self.outage,
        )

    async def call(self, method: str, *args: Any, tracker: Any = None, perms: Any = None) -> Any:
        async with self.factory() as session:
            service = ProxyPoolService(
                session,
                self.settings,
                tracker or self.tracker,
                permissions=perms,
                verdicts=self.verdicts,
                outage=self.outage,
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


async def test_parallel_assignment_respects_limit(factory: Any, redis: Any) -> None:
    pool = Pool(factory, redis, _settings(proxy_max_bots_per_ip=1))
    (proxy,), bots = await _seed(factory, 1, 2)
    results = await asyncio.gather(
        pool.call("assign_proxy", bots[0]),
        pool.call("assign_proxy", bots[1]),
        return_exceptions=True,
    )
    assert sorted(type(r).__name__ for r in results) == ["NoProxyAvailableError", "UUID"]
    rows = await pool.sql("SELECT count(*) FROM bots WHERE proxy_id = :p", p=proxy)
    assert rows[0][0] == 1


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
    assert [tuple(r) for r in audit] == [("proxy.strike", "system"), ("proxy.quarantine", "system")]
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
    assert pool.outage.value == 1
    rows = await pool.sql("SELECT sum(strike_count) FROM proxies")
    assert rows[0][0] == 0
    await pool.tracker.mark_ok(b)
    assert pool.outage.value == 0  # cleared by the first success


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
    rows = await pool.sql("SELECT sum(strike_count) FROM proxies")
    assert rows[0][0] == 0


async def test_manual_add_ban_activate(factory: Any, redis: Any) -> None:
    pool = Pool(factory, redis, _settings())
    perms = _Allow()
    _, (bot,) = await _seed(factory, 0, 1)
    proxy = await pool.call("add", "socks5://u:secret@10.8.0.1:1080", " main ", ACTOR, perms=perms)
    with pytest.raises(EntityAlreadyExistsError):
        await pool.call("add", "socks5://u:secret@10.8.0.1:1080", None, ACTOR, perms=perms)
    with pytest.raises(PermissionDeniedError):
        await pool.call("ban", proxy, ACTOR, perms=_Deny())
    assert await pool.call("assign_proxy", bot) == proxy
    assert await pool.call("ban", proxy, ACTOR, perms=perms) == (bot,)
    await pool.call("activate", proxy, ACTOR, perms=perms)
    assert set(perms.slugs) == {"proxies.manage"}
    rows = await pool.sql("SELECT status, label, url FROM proxies WHERE id = :p", p=proxy)
    assert rows[0][0] == "active" and rows[0][1] == "main"
    assert "secret" not in rows[0][2]  # encrypted at rest
    audit = await pool.sql(
        "SELECT action, actor_id, payload::text FROM audit_logs WHERE entity_id = :p "
        "ORDER BY created_at",
        p=proxy,
    )
    assert [r[0] for r in audit] == ["proxy.add", "proxy.ban", "proxy.activate"]
    assert all(r[1] == ACTOR and "secret" not in r[2] for r in audit)
