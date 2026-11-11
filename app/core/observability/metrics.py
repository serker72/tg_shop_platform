"""Prometheus metrics of one process.

Each process owns a dedicated :class:`CollectorRegistry` (not the ``prometheus_client``
global one), so tests can build isolated registries. The in-process
:class:`~app.modules.logging.metrics.LogsDroppedCounter` keeps its API; it is exported as
``logs_dropped_total{reason}`` by a collector reading its snapshot on every scrape.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import CounterMetricFamily
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector

from app.modules.logging.metrics import LogsDroppedCounter, logs_dropped_total

__all__ = [
    "METRICS_CONTENT_TYPE",
    "AppMetrics",
    "LogsDroppedCollector",
    "build_metrics",
    "render_metrics",
]

#: Content type of the text exposition format produced by :func:`generate_latest`.
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class LogsDroppedCollector(Collector):
    """Expose a :class:`LogsDroppedCounter` as ``logs_dropped_total{reason}``."""

    def __init__(self, counter: LogsDroppedCounter) -> None:
        self._counter = counter

    def collect(self) -> Iterable[Metric]:
        family = CounterMetricFamily(
            "logs_dropped",
            "Click-log records lost by the logging pipeline (written to stderr only).",
            labels=["reason"],
        )
        for reason, value in sorted(self._counter.by_reason.items()):
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


def build_metrics(dropped: LogsDroppedCounter | None = None) -> AppMetrics:
    """Create a fresh registry with all application metrics.

    ``dropped`` defaults to the process-wide :data:`logs_dropped_total` counter.
    """
    registry = CollectorRegistry(auto_describe=False)
    registry.register(LogsDroppedCollector(dropped if dropped is not None else logs_dropped_total))
    return AppMetrics(
        registry=registry,
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
    )


def render_metrics(registry: CollectorRegistry) -> bytes:
    """Render ``registry`` in the Prometheus text format."""
    return generate_latest(registry)
