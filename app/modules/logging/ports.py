"""Ports of the logging module.

Two different guarantees on purpose (TZ 47.7, 47.8):

* :class:`ActivityLogger` - click-log. Best effort: a logging failure must never break the
  user flow (a purchase), so ``log`` is synchronous, non-blocking and never raises. Lost
  records are reported to ``stderr`` and counted (``logs_dropped_total``).
* :class:`AuditLogger` - audit of money/permission changes. The record is part of the very
  business transaction: it is added to the caller's session and flushed; errors propagate,
  so a failed audit write rolls the business operation back instead of silently losing the
  audit trail.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.logging.events import ActivityEvent, AuditEvent

__all__ = ["ActivityLogger", "AuditLogger"]


class ActivityLogger(Protocol):
    """Click-log sink (TZ 47.1-47.4, 47.8)."""

    def log(self, event: ActivityEvent) -> None:
        """Enqueue ``event``; synchronous, non-blocking, never raises (best effort)."""
        ...


class AuditLogger(Protocol):
    """Audit sink (TZ 47.7): same transaction as the audited business operation."""

    async def record(self, session: AsyncSession, event: AuditEvent) -> None:
        """Add the audit row to ``session`` and flush (no commit); errors propagate."""
        ...
