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
from prometheus_client.core import CounterMetricFamily
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector

from app.core.version import VersionInfo, get_version_info
from app.modules.logging.metrics import DROP_REASONS, LogsDroppedCounter, logs_dropped_total

__all__ = [
    "METRICS_CONTENT_TYPE",
    "AppMetrics",
    "LogsDroppedCollector",
    "build_metrics",
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


def render_metrics(registry: CollectorRegistry) -> bytes:
    """Render ``registry`` in the Prometheus text format."""
    return generate_latest(registry)
