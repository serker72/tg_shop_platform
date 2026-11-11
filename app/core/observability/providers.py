"""Dishka provider of the observability components (needs Database/Redis providers)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol, assert_never, runtime_checkable

from dishka import Provider, Scope, provide
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.observability.health import HealthChecker, HeartbeatRegistry, build_health_checker
from app.core.observability.metrics import AppMetrics, build_metrics
from app.modules.logging.metrics import LogsDroppedCounter
from app.shared_kernel.domain.storage import StorageService
from app.shared_kernel.infrastructure.storage.minio import (
    MinIOStorage,
    build_minio_client,
    minio_client_config,
)
from app.shared_kernel.infrastructure.storage.s3 import S3Storage, build_s3_client, s3_client_config

__all__ = ["HealthProbeStorage", "ObservabilityProvider", "build_probe_storage_client"]


@runtime_checkable
class HealthProbeStorage(StorageService, Protocol):
    """DI key of the storage used only by the health probe (short timeouts, no retries)."""


def build_probe_storage_client(settings: Settings) -> Any:
    """``boto3`` client of the active backend with ``timeout = OBSERVABILITY_HEALTH_TIMEOUT``.

    The regular client (connect 10 s, read 60-120 s, 3 retries) would keep a probe thread
    busy for minutes on a dead backend; this one gives up after ~2 x timeout.
    """
    timeout = settings.observability.health_timeout_seconds
    backend = settings.storage.backend
    match backend:
        case "minio":
            config = minio_client_config(
                connect_timeout=timeout, read_timeout=timeout, max_retries=0
            )
            return build_minio_client(settings, config=config)
        case "s3":
            config = s3_client_config(connect_timeout=timeout, read_timeout=timeout, max_retries=0)
            return build_s3_client(settings, config=config)
        case _:
            assert_never(backend)


class ObservabilityProvider(Provider):
    """``AppMetrics``, ``HeartbeatRegistry``, probe storage and ``HealthChecker`` (APP).

    Needs ``LogsDroppedCounter`` (``LoggingProvider``): the same instance feeds
    ``LogBuffer`` and the ``logs_dropped_total`` series of ``/metrics``.
    """

    scope = Scope.APP

    @provide
    def provide_metrics(self, dropped: LogsDroppedCounter) -> AppMetrics:
        return build_metrics(dropped)

    @provide
    def provide_heartbeats(self) -> HeartbeatRegistry:
        return HeartbeatRegistry()

    @provide
    def provide_probe_storage(self, settings: Settings) -> Iterable[HealthProbeStorage]:
        client = build_probe_storage_client(settings)
        try:
            if settings.storage.backend == "s3":
                yield S3Storage(client=client, settings=settings)
            else:
                yield MinIOStorage(client=client, settings=settings)
        finally:
            client.close()

    @provide
    def provide_health_checker(
        self,
        settings: Settings,
        engine: AsyncEngine,
        redis: Redis,
        storage: HealthProbeStorage,
        heartbeats: HeartbeatRegistry,
    ) -> HealthChecker:
        return build_health_checker(
            settings, engine=engine, redis=redis, storage=storage, heartbeats=heartbeats
        )
