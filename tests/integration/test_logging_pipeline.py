"""Integration: ``LogBuffer`` -> partitioned ``user_activity`` + files; ``SqlAuditLogger``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.modules.logging.audit import SqlAuditLogger
from app.modules.logging.buffer import LogBuffer, read_file_logs_retention_days
from app.modules.logging.events import ActivityEvent, AuditEvent
from app.modules.logging.files import DailyFileWriter
from app.modules.logging.metrics import LogsDroppedCounter
from app.modules.logging.models import AuditLog, UserActivity
from app.shared_kernel.infrastructure.database import build_session_factory

pytestmark = pytest.mark.integration


async def test_buffer_writes_db_and_files(clean_db: AsyncEngine, tmp_path: Path) -> None:
    factory = build_session_factory(clean_db)
    counter = LogsDroppedCounter()
    buffer = LogBuffer(
        factory,
        DailyFileWriter(tmp_path),
        batch_size=3,
        flush_interval_seconds=0.05,
        prune_interval_seconds=None,
        counter=counter,
    )
    bot = uuid4()
    now = datetime.now(UTC)
    await buffer.start()
    for i in range(7):
        buffer.log(
            ActivityEvent(
                bot_id=bot,
                action_type="CLICK",
                telegram_user_id=100 + i % 2,
                metadata={"i": i},
                created_at=now,
            )
        )
    await buffer.stop(10)

    assert counter.value == 0
    async with factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(UserActivity).where(UserActivity.bot_id == bot)
        )
        meta = await session.scalar(
            select(UserActivity.metadata_).where(UserActivity.bot_id == bot).limit(1)
        )
        default_rows = await session.scalar(text("SELECT count(*) FROM user_activity_default"))
    assert count == 7
    assert isinstance(meta, dict)
    assert default_rows == 0  # rows landed in the monthly partition
    day = now.date().isoformat()
    assert (tmp_path / f"bots/bot_{bot}/{day}.log").read_text().count("\n") == 7
    assert (tmp_path / f"users/{day}/user_100.log").read_text().count("\n") == 4


async def test_poison_row_loses_only_itself(
    clean_db: AsyncEngine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RX3: one NOT NULL violation in a batch of 5 -> 4 rows stored, 1 ``db_rejected``."""
    factory = build_session_factory(clean_db)
    counter = LogsDroppedCounter()
    buffer = LogBuffer(
        factory,
        DailyFileWriter(tmp_path),
        batch_size=5,
        flush_interval_seconds=0.05,
        db_retry_backoff_seconds=0.0,
        prune_interval_seconds=None,
        counter=counter,
    )
    bot = uuid4()
    await buffer.start()
    for i in range(5):
        action = cast(str, None) if i == 2 else f"OK_{i}"
        buffer.log(ActivityEvent(bot_id=bot, action_type=action, telegram_user_id=i))
    await buffer.stop(10)

    async with factory() as session:
        actions = (
            await session.scalars(
                select(UserActivity.action_type).where(UserActivity.bot_id == bot)
            )
        ).all()
    assert sorted(actions) == ["OK_0", "OK_1", "OK_3", "OK_4"]
    assert counter.by_reason == {"db_rejected": 1}
    err = capsys.readouterr().err
    assert err.count("LOG_DROPPED reason=db_rejected | ") == 1
    assert "user=2" in err


async def test_audit_in_business_transaction(session: AsyncSession) -> None:
    logger = SqlAuditLogger()
    entity = uuid4()
    await logger.record(session, AuditEvent(actor_type="user", action="KEEP", entity_id=entity))
    nested = await session.begin_nested()
    await logger.record(session, AuditEvent(actor_type="user", action="UNDO", entity_id=entity))
    assert (
        await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == entity)
        )
        == 2
    )
    await nested.rollback()
    actions = (
        await session.scalars(select(AuditLog.action).where(AuditLog.entity_id == entity))
    ).all()
    assert actions == ["KEEP"]


async def test_file_logs_retention_column(session: AsyncSession) -> None:
    value = await session.scalar(
        text("SELECT file_logs_retention_days FROM system_settings WHERE id = 1")
    )
    assert value == 90
    nested = await session.begin_nested()
    with pytest.raises(IntegrityError):
        await session.execute(text("UPDATE system_settings SET file_logs_retention_days = 0"))
    await nested.rollback()


async def test_read_retention_days(clean_db: AsyncEngine) -> None:
    factory = build_session_factory(clean_db)
    async with factory.begin() as session:
        await session.execute(text("UPDATE system_settings SET file_logs_retention_days = 7"))
    assert await read_file_logs_retention_days(factory) == 7
