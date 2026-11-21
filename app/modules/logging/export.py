"""Streaming TXT export for activity logs."""

from __future__ import annotations

import asyncio
import gzip
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from app.modules.logging.events import ActivityEvent, AuditEvent
from app.modules.logging.files import format_line
from app.modules.logging.query import (
    ActivityFilter,
    ActivityQueryRepository,
    ActivityRow,
    ActivityScope,
    Order,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.modules.logging.ports import AuditLogger

MAX_EXPORT_ROWS = 100_000
GZIP_THRESHOLD = 45 * 1024 * 1024


def _event(row: ActivityRow) -> ActivityEvent:
    a = row.activity
    return ActivityEvent(
        telegram_bot_id=a.telegram_bot_id,
        action_type=a.action_type,
        tenant_id=a.tenant_id,
        telegram_user_id=a.telegram_user_id,
        username=a.username,
        user_type=a.user_type,
        role=a.role,
        source=a.source,
        entity_type=a.entity_type,
        entity_id=a.entity_id,
        order_id=a.order_id,
        product_id=a.product_id,
        category_id=a.category_id,
        subcategory_id=a.subcategory_id,
        case_id=a.case_id,
        preorder_id=a.preorder_id,
        button=a.button,
        callback_data=a.callback_data,
        screen_from=a.screen_from,
        screen_current=a.screen_current,
        screen_to=a.screen_to,
        metadata=a.metadata_,
        result=a.result,
        created_at=a.created_at,
    )


async def export_txt(
    filter: ActivityFilter,
    order: Order = "newest",
    *,
    repository: ActivityQueryRepository,
    logs_dir: str | Path,
    scope: ActivityScope,
    audit: Callable[[], Awaitable[None]] | None = None,
) -> Path:
    if scope is None:
        raise ValueError("activity export requires a server-generated ActivityScope")
    root = Path(logs_dir) / "system" / "tmp"
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    fd, raw_name = await asyncio.to_thread(
        tempfile.mkstemp, prefix="activity-", suffix=".txt", dir=root
    )
    await asyncio.to_thread(os.close, fd)
    raw = Path(raw_name)
    result = raw
    count = 0
    try:
        cursor = None
        stream = await asyncio.to_thread(raw.open, "w", encoding="utf-8", newline="\n")
        try:
            while count < MAX_EXPORT_ROWS:
                page = await repository.search(filter, order, cursor, scope=scope)
                for row in page.items:
                    if count >= MAX_EXPORT_ROWS:
                        break
                    await asyncio.to_thread(stream.write, format_line(_event(row)) + "\n")
                    count += 1
                if not page.next_cursor or not page.items:
                    break
                cursor = repository.decode_cursor(page.next_cursor)
        finally:
            await asyncio.to_thread(stream.close)
        size = (await asyncio.to_thread(raw.stat)).st_size
        if size > GZIP_THRESHOLD:
            gz = raw.with_suffix(raw.suffix + ".gz")
            result = gz
            await asyncio.to_thread(_gzip_file, raw, gz)
            await asyncio.to_thread(raw.unlink)
        if audit:
            await audit()
        return result
    except Exception:
        await asyncio.to_thread(raw.unlink, missing_ok=True)
        if result != raw:
            await asyncio.to_thread(result.unlink, missing_ok=True)
        raise


def _gzip_file(source: Path, target: Path) -> None:
    with source.open("rb") as src, gzip.open(target, "wb") as dst:
        while chunk := src.read(1024 * 1024):
            dst.write(chunk)


class ExportService:
    """Authorized export service with transactional audit record."""

    def __init__(
        self,
        repository: ActivityQueryRepository,
        audit_logger: AuditLogger,
        session: AsyncSession,
        logs_dir: str | Path,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.repository = repository
        self.audit_logger = audit_logger
        self.session = session
        self.logs_dir = logs_dir
        self.session_factory = session_factory

    async def _record_audit(self, event: AuditEvent) -> None:
        if self.session_factory is None:
            # Kept for lightweight callers/tests; the application always injects
            # a factory so export audit is committed independently.
            await self.audit_logger.record(self.session, event)
            return
        async with self.session_factory() as session:
            async with session.begin():
                await self.audit_logger.record(session, event)

    async def export_txt(
        self, actor: int, filter: ActivityFilter, order: Order, *, scope: ActivityScope
    ) -> Path:
        await self._record_audit(
            AuditEvent(
                actor_type="user", actor_id=actor, action="activity_export", result="started"
            )
        )
        try:
            path = await export_txt(
                filter,
                order,
                repository=self.repository,
                logs_dir=self.logs_dir,
                scope=scope,
            )
        except Exception as error:
            await self._record_audit(
                AuditEvent(
                    actor_type="user",
                    actor_id=actor,
                    action="activity_export",
                    result="failure",
                    payload={"error": type(error).__name__},
                )
            )
            raise
        return path

    async def record_delivery_success(self, actor: int) -> None:
        await self._record_audit(
            AuditEvent(
                actor_type="user", actor_id=actor, action="activity_export", result="success"
            )
        )

    async def record_delivery_failure(self, actor: int, error: Exception) -> None:
        await self._record_audit(
            AuditEvent(
                actor_type="user",
                actor_id=actor,
                action="activity_export",
                result="failure",
                payload={"error": type(error).__name__, "phase": "delivery"},
            )
        )


__all__ = ["ExportService", "GZIP_THRESHOLD", "MAX_EXPORT_ROWS", "export_txt"]
