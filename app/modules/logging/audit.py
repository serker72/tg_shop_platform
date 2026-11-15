"""SQL implementation of :class:`~app.modules.logging.ports.AuditLogger`."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.logging.events import AuditEvent
from app.modules.logging.models import AuditLog
from app.modules.logging.ports import AuditLogger

__all__ = ["SqlAuditLogger"]


class SqlAuditLogger(AuditLogger):
    """Write ``audit_logs`` rows inside the caller's transaction.

    The row is added to the passed session and flushed (so constraint errors surface right
    here), never committed: the business operation commits or rolls back both together.
    """

    async def record(self, session: AsyncSession, event: AuditEvent) -> None:
        """Add and flush one ``AuditLog``; errors propagate to the caller."""
        session.add(
            AuditLog(
                created_at=event.created_at,
                tenant_id=event.tenant_id,
                telegram_bot_id=event.telegram_bot_id,
                actor_id=event.actor_id,
                actor_type=event.actor_type,
                action=event.action,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                payload=dict(event.payload) if event.payload is not None else None,
                result=event.result,
            )
        )
        await session.flush()
