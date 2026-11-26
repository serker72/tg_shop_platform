"""Prometheus metrics of one process.

Each process owns a dedicated :class:`CollectorRegistry` (not the ``prometheus_client``
global one), so tests can build isolated registries. The in-process
:class:`~app.modules.logging.metrics.LogsDroppedCounter` keeps its API; it is exported as
``logs_dropped_total{reason}`` by a collector reading its snapshot on every scrape.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, Info, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector

from app.core.version import VersionInfo, get_version_info
from app.modules.bot_manager.metrics import (
    LabeledCounter,
    OutageState,
    proxy_failure_verdicts_total,
    proxy_probe_total,
    tenant_bots_stop_failures_total,
)
from app.modules.iam.metrics import (
    INVALIDATION_SCOPES,
    CacheInvalidationFailures,
    iam_cache_invalidation_failures_total,
)
from app.modules.logging.metrics import DROP_REASONS, LogsDroppedCounter, logs_dropped_total

__all__ = [
    "METRICS_CONTENT_TYPE",
    "AppMetrics",
    "IamCacheInvalidationCollector",
    "LogsDroppedCollector",
    "ProxyFailureCollector",
    "TelegramOutageCollector",
    "build_metrics",
    "register_component_metrics",
    "render_metrics",
    "set_build_info",
]

#: Content type of the text exposition format produced by :func:`generate_latest`.
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class LogsDroppedCollector(Collector):
    """Expose a :class:`LogsDroppedCounter` as ``logs_dropped_total{reason}``.

    Known reasons (:data:`DROP_REASONS`) are always present (``0`` before the first drop):
    a family without samples renders no series, and ``increase()`` would miss the first
    increment of a series that appears with a non-zero value.
    """

    def __init__(
        self, counter: LogsDroppedCounter, known_reasons: Iterable[str] = DROP_REASONS
    ) -> None:
        self._counter = counter
        self._known_reasons = tuple(known_reasons)

    @property
    def counter(self) -> LogsDroppedCounter:
        """The exported counter instance."""
        return self._counter

    def collect(self) -> Iterable[Metric]:
        family = CounterMetricFamily(
            "logs_dropped",
            "Click-log records lost by the logging pipeline (written to stderr only).",
            labels=["reason"],
        )
        values = dict.fromkeys(self._known_reasons, 0)
        values.update(self._counter.by_reason)
        for reason, value in sorted(values.items()):
            family.add_metric([reason], value)
        yield family

    def describe(self) -> Iterable[Metric]:
        return []


class IamCacheInvalidationCollector(Collector):
    """Expose :class:`CacheInvalidationFailures` as ``iam_cache_invalidation_failures_total``."""

    def __init__(self, counter: CacheInvalidationFailures) -> None:
        self._counter = counter

    def collect(self) -> Iterable[Metric]:
        family = CounterMetricFamily(
            "iam_cache_invalidation_failures",
            "Permission-cache invalidations not confirmed by Redis after a committed IAM change.",
            labels=["scope"],
        )
        values = dict.fromkeys(INVALIDATION_SCOPES, 0)
        values.update(self._counter.by_scope)
        for scope, value in sorted(values.items()):
            family.add_metric([scope], value)
        yield family

    def describe(self) -> Iterable[Metric]:
        return []


class ProxyFailureCollector(Collector):
    """``proxy_failure_verdicts_total{verdict}`` and ``proxy_probe_total{result}`` of this
    process (``bot_worker``); every label value is exported with ``0``."""

    def __init__(self, verdicts: LabeledCounter, probes: LabeledCounter) -> None:
        self._verdicts = verdicts
        self._probes = probes

    def collect(self) -> Iterable[Metric]:
        verdicts = CounterMetricFamily(
            "proxy_failure_verdicts",
            "Decisions on proxy failures (strike / outage / single_proxy / deduplicated / error).",
            labels=["verdict"],
        )
        for verdict, value in sorted(self._verdicts.snapshot().items()):
            verdicts.add_metric([verdict], value)
        yield verdicts
        probes = CounterMetricFamily(
            "proxy_probe", "Proxy handshake probes by result.", labels=["result"]
        )
        for result, value in sorted(self._probes.snapshot().items()):
            probes.add_metric([result], value)
        yield probes

    def describe(self) -> Iterable[Metric]:
        return []


class TenantBotsStopFailuresCollector(Collector):
    def collect(self) -> Iterable[Metric]:
        yield CounterMetricFamily(
            "tenant_bots_stop_failures",
            "Failed bot stop transactions after a committed shop change.",
            value=tenant_bots_stop_failures_total.snapshot()["failure"],
        )

    def describe(self) -> Iterable[Metric]:
        return []


class TelegramOutageCollector(Collector):
    """``telegram_outage`` (0/1) from :class:`OutageState` (``master_bot`` only).

    Reads only the in-memory cache filled by the background ``OutageMonitor``: the scrape
    never blocks on Redis. No value yet / older than ``2 x poll interval`` -> the series is
    omitted (``absent(telegram_outage)``), the rest of ``/metrics`` is served.
    """

    def __init__(self, state: OutageState) -> None:
        self._state = state

    def collect(self) -> Iterable[Metric]:
        value = self._state.current()
        if value is None:
            return
        yield GaugeMetricFamily(
            "telegram_outage",
            "1 - every proxy fails at once (Telegram unreachable), reset by the first success.",
            value=value,
        )

    def describe(self) -> Iterable[Metric]:
        return []


@dataclass(frozen=True, slots=True)
class AppMetrics:
    """Registry and the application metrics registered in it."""

    registry: CollectorRegistry
    app_errors_total: Counter
    update_duration_seconds: Histogram
    active_bots: Gauge
    logs_dropped: LogsDroppedCounter
    #: ``app_build_info{version, commit, component} 1`` (one series per process).
    build_info: Info


def set_build_info(
    metrics: AppMetrics, *, component: str, version_info: VersionInfo | None = None
) -> None:
    """(Re)set ``app_build_info`` labels; low cardinality: one series per running build."""
    info = version_info if version_info is not None else get_version_info()
    metrics.build_info.info(
        {"version": info.version, "commit": info.commit or "", "component": component}
    )


def build_metrics(
    dropped: LogsDroppedCounter | None = None, *, component: str = "unknown"
) -> AppMetrics:
    """Create a fresh registry with all application metrics.

    ``dropped`` defaults to the process-wide :data:`logs_dropped_total` counter;
    ``component`` labels ``app_build_info`` (``run_service`` sets the service name).
    """
    counter = dropped if dropped is not None else logs_dropped_total
    registry = CollectorRegistry(auto_describe=False)
    registry.register(LogsDroppedCollector(counter))
    registry.register(IamCacheInvalidationCollector(iam_cache_invalidation_failures_total))
    metrics = AppMetrics(
        registry=registry,
        logs_dropped=counter,
        app_errors_total=Counter(
            "app_errors",
            "Unhandled application errors.",
            labelnames=("component",),
            registry=registry,
        ),
        update_duration_seconds=Histogram(
            "update_duration_seconds",
            "Telegram update processing time, seconds.",
            registry=registry,
        ),
        active_bots=Gauge(
            "active_bots",
            "Tenant bots currently running in this process.",
            registry=registry,
        ),
        build_info=Info(
            "app_build", "Version of the running application build.", registry=registry
        ),
    )
    set_build_info(metrics, component=component)
    return metrics


def register_component_metrics(
    metrics: AppMetrics, *, component: str, outage_state: OutageState | None = None
) -> None:
    """Register the collectors owned by a process role (no duplicate series elsewhere).

    ``bot_worker`` - proxy failure verdicts and probes (aggregate with ``sum`` across
    replicas); ``master_bot`` - the shared ``telegram_outage`` gauge (needs ``outage_state``).
    """
    if component == "bot_worker":
        metrics.registry.register(
            ProxyFailureCollector(proxy_failure_verdicts_total, proxy_probe_total)
        )
    elif component == "master_bot":
        metrics.registry.register(TenantBotsStopFailuresCollector())
        if outage_state is not None:
            metrics.registry.register(TelegramOutageCollector(outage_state))


def render_metrics(registry: CollectorRegistry) -> bytes:
    """Render ``registry`` in the Prometheus text format."""
    return generate_latest(registry)
