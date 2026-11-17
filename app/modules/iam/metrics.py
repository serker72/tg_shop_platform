"""In-process metrics of the IAM module.

``iam_cache_invalidation_failures_total{scope}`` counts permission-cache invalidations that
Redis did not confirm after a committed IAM change (``scope`` = ``user`` | ``all``; ``settings``
- the ``system_settings`` snapshot of ``SystemSettingsCache``): the
change is stored, but other processes may serve the old snapshot until its TTL. Exported by
:class:`~app.core.observability.metrics.IamCacheInvalidationCollector`.
"""

from __future__ import annotations

import threading

__all__ = [
    "INVALIDATION_SCOPES",
    "CacheInvalidationFailures",
    "iam_cache_invalidation_failures_total",
]

#: Every ``scope`` label value (exported with ``0`` before the first failure).
INVALIDATION_SCOPES: tuple[str, ...] = ("all", "settings", "user")


class CacheInvalidationFailures:
    """Thread-safe monotonic counter with a ``scope`` label."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_scope: dict[str, int] = {}

    def inc(self, n: int = 1, *, scope: str) -> None:
        if n <= 0:
            return
        with self._lock:
            self._by_scope[scope] = self._by_scope.get(scope, 0) + n

    @property
    def value(self) -> int:
        with self._lock:
            return sum(self._by_scope.values())

    @property
    def by_scope(self) -> dict[str, int]:
        with self._lock:
            return dict(self._by_scope)

    def reset(self) -> None:
        """Tests only."""
        with self._lock:
            self._by_scope.clear()


#: Process-wide counter.
iam_cache_invalidation_failures_total = CacheInvalidationFailures()
