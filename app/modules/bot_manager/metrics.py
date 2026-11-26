"""In-process counters of the proxy pool and the shared outage key.

* ``proxy_failure_verdicts_total{verdict}`` - decisions of ``ProxyPoolService.report_failure``;
* ``proxy_probe_total{result}`` - results of ``probe_handshake``.

Both are incremented and exported by the process that runs the bots (``bot_worker``);
Prometheus aggregates them with ``sum``. Every label value is exported with ``0``.

The Telegram outage state is shared between processes: Redis key :data:`OUTAGE_KEY`
(set by an ``outage`` verdict with TTL = strike window, deleted by the first successful
``mark_ok``). Only ``master_bot`` exports it as the ``telegram_outage`` gauge
(:class:`~app.core.observability.metrics.TelegramOutageCollector`): a background task
(:class:`~app.modules.bot_manager.proxy_health.OutageMonitor`) reads the key every
:data:`OUTAGE_POLL_INTERVAL_SECONDS` into :class:`OutageState`; the scrape only reads that
cache and never touches Redis.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from typing import Final

__all__ = [
    "FAILURE_VERDICTS",
    "OUTAGE_KEY",
    "PROBE_RESULTS",
    "OUTAGE_POLL_INTERVAL_SECONDS",
    "LabeledCounter",
    "OutageState",
    "proxy_failure_verdicts_total",
    "proxy_probe_total",
    "tenant_bots_stop_failures_total",
]

FAILURE_VERDICTS: tuple[str, ...] = ("strike", "outage", "single_proxy", "deduplicated", "error")
PROBE_RESULTS: tuple[str, ...] = ("ok", "proxy_dead", "telegram_blocked")
#: Redis key of a suspected Telegram outage (value - unix ts, TTL - strike window).
OUTAGE_KEY: Final = "proxy:outage"
#: Poll period of ``proxy:outage`` in ``master_bot``; the cached value is served while it is
#: younger than twice the period, otherwise ``telegram_outage`` is absent.
OUTAGE_POLL_INTERVAL_SECONDS: Final = 15.0


class LabeledCounter:
    """Thread-safe monotonic counter with one label and a fixed set of known values."""

    def __init__(self, values: Iterable[str]) -> None:
        self._lock = threading.Lock()
        self._known = tuple(values)
        self._values: dict[str, int] = dict.fromkeys(self._known, 0)

    def inc(self, value: str, n: int = 1) -> None:
        if n <= 0:
            return
        with self._lock:
            self._values[value] = self._values.get(value, 0) + n

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)

    def reset(self) -> None:
        """Tests only."""
        with self._lock:
            self._values = dict.fromkeys(self._known, 0)


class OutageState:
    """Last ``EXISTS proxy:outage`` result with its age (thread-safe, monotonic clock).

    :meth:`current` is ``None`` before the first successful check or when the value is
    older than ``max_age_seconds`` (Redis unreachable / the poller stopped).
    """

    def __init__(
        self,
        *,
        max_age_seconds: float = 2 * OUTAGE_POLL_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._max_age = max_age_seconds
        self._clock = clock
        self._value: int | None = None
        self._checked_at = 0.0

    def update(self, active: bool) -> None:
        with self._lock:
            self._value = 1 if active else 0
            self._checked_at = self._clock()

    def current(self) -> int | None:
        with self._lock:
            if self._value is None or self._clock() - self._checked_at > self._max_age:
                return None
            return self._value


proxy_failure_verdicts_total = LabeledCounter(FAILURE_VERDICTS)
proxy_probe_total = LabeledCounter(PROBE_RESULTS)
# Incremented when the IAM mutation succeeded but the bot stop transaction failed.
tenant_bots_stop_failures_total = LabeledCounter(("failure",))
