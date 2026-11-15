"""In-process counters of the proxy pool and the shared outage key.

* ``proxy_failure_verdicts_total{verdict}`` - decisions of ``ProxyPoolService.report_failure``;
* ``proxy_probe_total{result}`` - results of ``probe_handshake``.

Both are incremented and exported by the process that runs the bots (``bot_worker``);
Prometheus aggregates them with ``sum``. Every label value is exported with ``0``.

The Telegram outage state is shared between processes: Redis key :data:`OUTAGE_KEY`
(set by an ``outage`` verdict with TTL = strike window, deleted by the first successful
``mark_ok``). Only ``master_bot`` exports it as the ``telegram_outage`` gauge
(:class:`~app.core.observability.metrics.TelegramOutageCollector`).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Final

__all__ = [
    "FAILURE_VERDICTS",
    "OUTAGE_KEY",
    "PROBE_RESULTS",
    "LabeledCounter",
    "proxy_failure_verdicts_total",
    "proxy_probe_total",
]

FAILURE_VERDICTS: tuple[str, ...] = ("strike", "outage", "single_proxy", "deduplicated", "error")
PROBE_RESULTS: tuple[str, ...] = ("ok", "proxy_dead", "telegram_blocked")
#: Redis key of a suspected Telegram outage (value - unix ts, TTL - strike window).
OUTAGE_KEY: Final = "proxy:outage"


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


proxy_failure_verdicts_total = LabeledCounter(FAILURE_VERDICTS)
proxy_probe_total = LabeledCounter(PROBE_RESULTS)
