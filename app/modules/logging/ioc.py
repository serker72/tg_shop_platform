"""Dishka provider of the logging module.

Requires ``Settings`` and ``async_sessionmaker[AsyncSession]`` (``DatabaseProvider``).
"""

from __future__ import annotations

from collections.abc import AsyncIterable

from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.modules.logging.audit import SqlAuditLogger
from app.modules.logging.buffer import LogBuffer
from app.modules.logging.export import ExportService
from app.modules.logging.metrics import LogsDroppedCounter, logs_dropped_total
from app.modules.logging.ports import ActivityLogger, AuditLogger
from app.modules.logging.query import ActivityQueryRepository

__all__ = ["LoggingProvider"]


class LoggingProvider(Provider):
    """``ActivityLogger`` (started ``LogBuffer``), ``AuditLogger`` and the drop counter."""

    scope = Scope.APP

    @provide
    def provide_dropped_counter(self) -> LogsDroppedCounter:
        """Return the process-wide ``logs_dropped_total`` counter."""
        return logs_dropped_total

    @provide
    async def provide_activity_logger(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        counter: LogsDroppedCounter,
    ) -> AsyncIterable[ActivityLogger]:
        """Start the buffer; drain and stop it when the container closes."""
        buffer = LogBuffer.from_settings(settings, session_factory, counter=counter)
        await buffer.start()
        try:
            yield buffer
        finally:
            await buffer.stop(settings.logs.buffer_shutdown_timeout_seconds)

    @provide(scope=Scope.REQUEST)
    def provide_activity_query_repository(self, session: AsyncSession) -> ActivityQueryRepository:
        return ActivityQueryRepository(session)

    @provide(scope=Scope.REQUEST)
    def provide_export_service(
        self,
        repository: ActivityQueryRepository,
        audit_logger: AuditLogger,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> ExportService:
        return ExportService(repository, audit_logger, session, settings.logs.dir, session_factory)

    @provide
    def provide_audit_logger(self) -> AuditLogger:
        """Return the SQL audit logger (writes into the caller's session)."""
        return SqlAuditLogger()
