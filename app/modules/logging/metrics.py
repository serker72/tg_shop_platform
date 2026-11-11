"""In-process metrics of the logging pipeline.

``logs_dropped_total`` counts click-log records that were lost (written to ``stderr`` only).
It is exported to Prometheus as ``logs_dropped_total{reason=...}`` by
:class:`~app.core.observability.metrics.LogsDroppedCollector` (reads :attr:`by_reason`
on every scrape); DI shares one instance between ``LogBuffer`` and ``AppMetrics``.
"""

from __future__ import annotations

import threading

__all__ = ["DROP_REASONS", "LogsDroppedCounter", "logs_dropped_total"]

#: Every ``reason`` used by :class:`~app.modules.logging.buffer.LogBuffer`. The exporter
#: emits them with value ``0`` before the first drop, so ``logs_dropped_total{reason}``
#: exists from the process start and ``increase(...)`` sees the first increment.
DROP_REASONS: tuple[str, ...] = (
    "db_error",
    "db_rejected",
    "file_error",
    "internal_error",
    "not_running",
    "queue_full",
    "shutdown_timeout",
)


class LogsDroppedCounter:
    """Thread-safe monotonic counter with a ``reason`` label."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_reason: dict[str, int] = {}

    def inc(self, n: int = 1, *, reason: str) -> None:
        """Add ``n`` (>= 0) dropped records for ``reason``."""
        if n <= 0:
            return
        with self._lock:
            self._by_reason[reason] = self._by_reason.get(reason, 0) + n

    @property
    def value(self) -> int:
        """Total dropped records over all reasons."""
        with self._lock:
            return sum(self._by_reason.values())

    @property
    def by_reason(self) -> dict[str, int]:
        """Snapshot of the per-reason values."""
        with self._lock:
            return dict(self._by_reason)

    def reset(self) -> None:
        """Reset all values (tests only)."""
        with self._lock:
            self._by_reason.clear()


#: Process-wide counter (one per process: ``master_bot`` and ``bot_worker`` count separately).
logs_dropped_total = LogsDroppedCounter()
