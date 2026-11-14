"""Readiness checks of the infrastructure and worker heartbeats.

Every check runs with its own timeout and never raises: a failure or timeout becomes a
:class:`CheckResult`. ``ready`` is ``True`` when every *critical* check is ok; a failed
non-critical check only marks the report ``degraded``. :class:`HealthReportCache` bounds
how often the probes actually hit the infrastructure (``/health/ready`` uses it).

Heartbeat contract: one Redis ZSET per component, ``heartbeat:{component}``; member =
instance id, score = unix timestamp of the last beat. :func:`publish_heartbeat` does
``ZADD`` + ``ZREMRANGEBYSCORE(-inf, now - ttl)`` (drops dead instances) + ``EXPIRE`` (the
whole key disappears when every instance is gone). A component is alive while
``ZCOUNT(now - ttl, +inf) > 0``. Components are declared in a :class:`HeartbeatRegistry`;
only the ones declared ``required`` affect ``ready``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import aio_pika
from opentelemetry.instrumentation.utils import suppress_instrumentation
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.shared_kernel.domain.storage import StorageService

__all__ = [
    "HEARTBEAT_KEY_PREFIX",
    "CheckResult",
    "CheckStatus",
    "HealthCheck",
    "HealthChecker",
    "HealthReport",
    "HealthReportCache",
    "HeartbeatMissingError",
    "HeartbeatRegistry",
    "build_health_checker",
    "has_live_heartbeat",
    "heartbeat_key",
    "postgres_probe",
    "publish_heartbeat",
    "rabbitmq_probe",
    "redis_probe",
    "storage_probe",
]

HEARTBEAT_KEY_PREFIX = "heartbeat"

#: ``degraded`` - a non-critical check failed or timed out.
CheckStatus = Literal["ok", "fail", "timeout", "degraded"]
Probe = Callable[[], Awaitable[None]]


class HeartbeatMissingError(Exception):
    """No live heartbeat of the component."""


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """A named probe; ``critical`` probes decide readiness."""

    name: str
    probe: Probe
    critical: bool = True


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    critical: bool
    duration_ms: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True, slots=True)
class HealthReport:
    ready: bool
    checks: tuple[CheckResult, ...]

    @property
    def status(self) -> Literal["ok", "degraded", "fail"]:
        if not self.ready:
            return "fail"
        return "ok" if all(check.ok for check in self.checks) else "degraded"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ready": self.ready,
            "checks": {
                check.name: {
                    "status": check.status,
                    "critical": check.critical,
                    "duration_ms": round(check.duration_ms, 2),
                    "error": check.error,
                }
                for check in self.checks
            },
        }


class HealthChecker:
    """Run all checks concurrently, each bounded by ``timeout`` seconds."""

    def __init__(self, checks: Sequence[HealthCheck], *, timeout: float) -> None:
        self._checks = tuple(checks)
        self._timeout = timeout

    @property
    def checks(self) -> tuple[HealthCheck, ...]:
        return self._checks

    async def run(self) -> HealthReport:
        results = await asyncio.gather(*(self._run_one(check) for check in self._checks))
        ready = all(result.ok for result in results if result.critical)
        return HealthReport(ready=ready, checks=tuple(results))

    async def _run_one(self, check: HealthCheck) -> CheckResult:
        started = time.monotonic()
        status: CheckStatus = "ok"
        error: str | None = None
        try:
            async with asyncio.timeout(self._timeout):
                await check.probe()
        except TimeoutError:
            status, error = "timeout", f"timed out after {self._timeout}s"
        except Exception as exc:  # noqa: BLE001 - a probe must never break the endpoint
            # Only the type: messages may contain DSNs/credentials.
            status, error = "fail", type(exc).__name__
        if status != "ok" and not check.critical:
            status = "degraded"
        duration_ms = (time.monotonic() - started) * 1000
        return CheckResult(check.name, status, check.critical, duration_ms, error)


class HealthReportCache:
    """Reuse the last report for ``ttl`` seconds; concurrent callers share one run.

    Bounds the probe rate (one AMQP connection, one S3 HEAD, ... per ``ttl``) no matter
    how often ``/health/ready`` is polled. ``ttl <= 0`` disables caching (callers are
    still serialised).
    """

    def __init__(
        self,
        checker: HealthChecker,
        *,
        ttl: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._checker = checker
        self._ttl = ttl
        self._clock = clock
        self._lock = asyncio.Lock()
        self._report: HealthReport | None = None
        self._expires_at = 0.0

    async def get(self) -> HealthReport:
        if self._report is not None and self._clock() < self._expires_at:
            return self._report
        async with self._lock:
            # Another caller may have refreshed the report while we waited.
            if self._report is not None and self._clock() < self._expires_at:
                return self._report
            report = await self._checker.run()
            self._report = report
            self._expires_at = self._clock() + self._ttl
            return report


def postgres_probe(engine: AsyncEngine) -> Probe:
    async def probe() -> None:
        # No OTel spans for probes: /health/ready would create a root trace per request.
        with suppress_instrumentation():
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))

    return probe


def redis_probe(redis: Redis) -> Probe:
    async def probe() -> None:
        await redis.ping()

    return probe


def storage_probe(storage: StorageService, key: str = "health/probe") -> Probe:
    """HEAD of a probe object: a missing bucket or an unreachable backend raises.

    ``storage`` must use a client with short connect/read timeouts and no retries
    (see ``ObservabilityProvider``): the SDK call runs in a worker thread that
    ``asyncio.timeout`` cannot interrupt, so only the client's own timeouts bound it.
    """

    async def probe() -> None:
        await storage.exists(key)

    return probe


def rabbitmq_probe(url: str, *, timeout: float) -> Probe:
    """Open and close an AMQP connection (TCP + AMQP handshake + auth).

    A new connection per probe is deliberate: it verifies the broker as a fresh client
    sees it and keeps no long-lived connection in the health component. The probe rate is
    bounded by :class:`HealthReportCache` (one connection per cache TTL at most).
    """

    async def probe() -> None:
        connection = await aio_pika.connect(url, timeout=timeout)
        await connection.close()

    return probe


def heartbeat_key(component: str) -> str:
    """Redis ZSET key holding the heartbeats of every instance of ``component``."""
    return f"{HEARTBEAT_KEY_PREFIX}:{component}"


async def publish_heartbeat(
    redis: Redis,
    component: str,
    instance: str,
    *,
    ttl: int,
    now: float | None = None,
) -> None:
    """Record a beat of ``instance`` and drop instances silent for longer than ``ttl``."""
    ts = time.time() if now is None else now
    key = heartbeat_key(component)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zadd(key, {instance: ts})
        pipe.zremrangebyscore(key, "-inf", ts - ttl)
        pipe.expire(key, ttl)
        await pipe.execute()


async def has_live_heartbeat(
    redis: Redis, component: str, *, ttl: int, now: float | None = None
) -> bool:
    """Return ``True`` if some instance of ``component`` beat within the last ``ttl`` s."""
    ts = time.time() if now is None else now
    return int(await redis.zcount(heartbeat_key(component), ts - ttl, "+inf")) > 0


@dataclass(slots=True)
class HeartbeatRegistry:
    """Declared worker components: name -> ``required`` (affects readiness)."""

    components: dict[str, bool] = field(default_factory=dict)

    def register(self, component: str, *, required: bool = False) -> None:
        self.components[component] = required

    def checks(self, redis: Redis, *, ttl: int) -> list[HealthCheck]:
        return [
            HealthCheck(f"heartbeat:{name}", _heartbeat_probe(redis, name, ttl), critical=required)
            for name, required in sorted(self.components.items())
        ]


def _heartbeat_probe(redis: Redis, component: str, ttl: int) -> Probe:
    async def probe() -> None:
        if not await has_live_heartbeat(redis, component, ttl=ttl):
            raise HeartbeatMissingError(component)

    return probe


def build_health_checker(
    settings: Settings,
    *,
    engine: AsyncEngine,
    redis: Redis,
    storage: StorageService,
    heartbeats: HeartbeatRegistry | None = None,
    critical: Mapping[str, bool] | None = None,
) -> HealthChecker:
    """Standard checks: ``postgres``, ``redis``, ``storage``, ``rabbitmq`` + heartbeats.

    ``storage`` should be the dedicated short-timeout probe storage (see
    :func:`storage_probe`). ``critical`` overrides the criticality of the standard checks
    (all critical by default).
    """
    obs = settings.observability
    timeout = obs.health_timeout_seconds
    flags = dict(critical or {})
    checks = [
        HealthCheck("postgres", postgres_probe(engine), flags.get("postgres", True)),
        HealthCheck("redis", redis_probe(redis), flags.get("redis", True)),
        HealthCheck("storage", storage_probe(storage), flags.get("storage", True)),
        HealthCheck(
            "rabbitmq",
            rabbitmq_probe(settings.rabbitmq_url, timeout=timeout),
            flags.get("rabbitmq", True),
        ),
    ]
    if heartbeats is not None:
        checks.extend(heartbeats.checks(redis, ttl=obs.heartbeat_ttl_seconds))
    return HealthChecker(checks, timeout=timeout)
