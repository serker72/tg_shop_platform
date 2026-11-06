"""Unit tests for :class:`app.modules.logging.audit.SqlAuditLogger`."""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.logging.audit import SqlAuditLogger
from app.modules.logging.events import AuditEvent
from app.modules.logging.models import AuditLog


class FakeSession:
    def __init__(self, fail: bool = False) -> None:
        self.added: list[Any] = []
        self.flushed = 0
        self.committed = False
        self.fail = fail

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1
        if self.fail:
            raise RuntimeError("constraint")

    async def commit(self) -> None:  # pragma: no cover - must not be called
        self.committed = True


async def test_record_adds_and_flushes_without_commit() -> None:
    session = FakeSession()
    entity = uuid4()
    event = AuditEvent(
        actor_type="user",
        action="BALANCE_CHANGE",
        actor_id=42,
        entity_type="order",
        entity_id=entity,
        payload={"amount": "10.00"},
    )
    await SqlAuditLogger().record(cast(AsyncSession, session), event)
    assert session.flushed == 1 and not session.committed
    (row,) = session.added
    assert isinstance(row, AuditLog)
    assert row.actor_id == 42 and row.entity_id == entity
    assert row.payload == {"amount": "10.00"}
    assert row.created_at == event.created_at


async def test_record_propagates_errors() -> None:
    with pytest.raises(RuntimeError, match="constraint"):
        await SqlAuditLogger().record(
            cast(AsyncSession, FakeSession(fail=True)),
            AuditEvent(actor_type="system", action="X"),
        )
