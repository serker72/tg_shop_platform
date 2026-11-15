"""In-process metrics of the proxy pool, exported by
:class:`~app.core.observability.metrics.ProxyPoolCollector`:

* ``proxy_failure_verdicts_total{verdict}`` - decisions of ``ProxyPoolService.report_failure``;
* ``proxy_probe_total{result}`` - results of ``probe_handshake``;
* ``telegram_outage`` (gauge 0/1) - set by an ``outage`` verdict, reset by the first
  confirmed ``mark_ok`` (source of the ``TelegramUnreachable`` alert, task 13).

Every label value is exported with ``0`` before its first event.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

__all__ = [
    "FAILURE_VERDICTS",
    "PROBE_RESULTS",
    "LabeledCounter",
    "OutageGauge",
    "proxy_failure_verdicts_total",
    "proxy_probe_total",
    "telegram_outage",
]

FAILURE_VERDICTS: tuple[str, ...] = ("strike", "outage", "single_proxy", "deduplicated", "error")
PROBE_RESULTS: tuple[str, ...] = ("ok", "proxy_dead", "telegram_blocked")


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


class OutageGauge:
    """0/1 gauge."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def set(self, value: int) -> None:
        with self._lock:
            self._value = 1 if value else 0

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


proxy_failure_verdicts_total = LabeledCounter(FAILURE_VERDICTS)
proxy_probe_total = LabeledCounter(PROBE_RESULTS)
telegram_outage = OutageGauge()
