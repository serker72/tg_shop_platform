"""Health checks and heartbeats against the real test stack (PG, Redis, MinIO, RabbitMQ)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.observability.health import (
    HeartbeatRegistry,
    build_health_checker,
    has_live_heartbeat,
    heartbeat_key,
    publish_heartbeat,
)
from app.core.observability.providers import build_probe_storage_client
from app.shared_kernel.infrastructure.database import build_engine
from app.shared_kernel.infrastructure.storage.minio import MinIOStorage, build_minio_client
from tests.integration._guards import assert_test_database

pytestmark = [pytest.mark.integration, pytest.mark.req("R8")]

COMPONENT = "obs_test_component"


def _ensure_bucket(settings: Settings) -> None:
    client = build_minio_client(settings)
    bucket = settings.storage.resolved_bucket(settings.minio)
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:  # noqa: BLE001 - fresh stack has no bucket yet
        client.create_bucket(Bucket=bucket)
    finally:
        client.close()


@pytest.fixture
async def heartbeat_redis(redis_client: Any) -> AsyncIterator[Any]:
    await redis_client.unlink(heartbeat_key(COMPONENT))
    yield redis_client
    await redis_client.unlink(heartbeat_key(COMPONENT))


@pytest.mark.req("R1")
async def test_ready_against_real_stack(
    settings: Settings, migrated_db: Any, heartbeat_redis: Any
) -> None:
    assert_test_database(settings)
    engine = build_engine(settings, poolclass=NullPool)
    _ensure_bucket(settings)
    storage = MinIOStorage(client=build_probe_storage_client(settings), settings=settings)
    registry = HeartbeatRegistry()
    registry.register(COMPONENT)
    checker = build_health_checker(
        settings, engine=engine, redis=heartbeat_redis, storage=storage, heartbeats=registry
    )
    try:
        report = await checker.run()
        statuses = {c.name: c.status for c in report.checks}
        assert statuses == {
            "postgres": "ok",
            "redis": "ok",
            "storage": "ok",
            "rabbitmq": "ok",
            f"heartbeat:{COMPONENT}": "degraded",
        }, report.as_dict()
        assert report.ready is True

        await publish_heartbeat(heartbeat_redis, COMPONENT, "i-1", ttl=10)
        key = heartbeat_key(COMPONENT)
        assert await heartbeat_redis.type(key) == b"zset"
        assert 0 < await heartbeat_redis.ttl(key) <= 10
        report = await checker.run()
        assert report.status == "ok", report.as_dict()
    finally:
        await engine.dispose()


async def test_heartbeat_zset_prunes_dead_instances(heartbeat_redis: Any) -> None:
    key = heartbeat_key(COMPONENT)
    await publish_heartbeat(heartbeat_redis, COMPONENT, "dead", ttl=10, now=1000.0)
    await publish_heartbeat(heartbeat_redis, COMPONENT, "alive", ttl=10, now=1020.0)
    members = await heartbeat_redis.zrange(key, 0, -1, withscores=True)
    assert members == [(b"alive", 1020.0)]
    assert await has_live_heartbeat(heartbeat_redis, COMPONENT, ttl=10, now=1025.0)
    assert not await has_live_heartbeat(heartbeat_redis, COMPONENT, ttl=10, now=1031.0)


async def test_rabbitmq_bad_credentials_fail(settings: Settings, heartbeat_redis: Any) -> None:
    bad = settings.model_copy(
        update={"rabbitmq": settings.rabbitmq.model_copy(update={"password": SecretStr("x")})}
    )
    engine = build_engine(settings, poolclass=NullPool)
    storage = MinIOStorage(client=build_probe_storage_client(settings), settings=settings)
    try:
        report = await build_health_checker(
            bad, engine=engine, redis=heartbeat_redis, storage=storage
        ).run()
        rabbit = next(c for c in report.checks if c.name == "rabbitmq")
        assert rabbit.status in {"fail", "timeout"}
        assert report.ready is False
        assert not await has_live_heartbeat(heartbeat_redis, COMPONENT, ttl=10)
    finally:
        await engine.dispose()
