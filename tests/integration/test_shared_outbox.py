"""Shared outbox migration compatibility against the isolated PostgreSQL stack."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from faststream_outbox.client import OutboxClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.shared_kernel.infrastructure.outbox import outbox_dlq, outbox_messages

pytestmark = pytest.mark.integration


async def test_shared_outbox_schema_is_package_compatible(engine: AsyncEngine) -> None:
    """The migration exposes the faststream-outbox transport schema unchanged."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'outbox_messages'
                ORDER BY ordinal_position
                """
            )
        )
        columns = [str(row[0]) for row in result]

    assert columns
    for required in ("id", "queue", "payload", "headers", "created_at"):
        assert required in columns
    assert "tenant_id" not in columns
    assert "idempotency_key" not in columns


async def _insert_message(engine: AsyncEngine) -> int:
    async with engine.begin() as connection:
        result = await connection.execute(
            outbox_messages.insert()
            .values(
                queue="integration.outbox",
                payload=b"{}",
                headers={"content-type": "application/json"},
                timer_id="integration.timer",
            )
            .returning(outbox_messages.c.id)
        )
        return int(result.scalar_one())


@pytest.mark.integration
async def test_settlement_is_durable_after_writer_connection_closes(engine: AsyncEngine) -> None:
    client = OutboxClient(engine, outbox_messages, dlq_table=outbox_dlq)
    row_id = await _insert_message(engine)
    async with engine.connect() as claim:
        rows = await client.fetch(claim, ["integration.outbox"], limit=1, lease_ttl_seconds=120)
    assert rows and rows[0].id == row_id
    token = rows[0].acquired_token
    assert token is not None
    async with engine.connect() as writer_base:
        writer = await writer_base.execution_options(isolation_level="AUTOCOMMIT")
        assert await client.delete_with_lease(writer, row_id, token) is True
    async with engine.connect() as connection:
        assert (
            await connection.execute(
                text("SELECT count(*) FROM outbox_messages WHERE id=:id"), {"id": row_id}
            )
        ).scalar_one() == 0


@pytest.mark.integration
async def test_retry_and_dlq_settlement_are_durable(engine: AsyncEngine) -> None:
    client = OutboxClient(engine, outbox_messages, dlq_table=outbox_dlq)
    row_id = await _insert_message(engine)
    async with engine.connect() as claim:
        row = (await client.fetch(claim, ["integration.outbox"], limit=1, lease_ttl_seconds=120))[0]
    token = row.acquired_token
    assert token is not None
    async with engine.connect() as writer_base:
        writer = await writer_base.execution_options(isolation_level="AUTOCOMMIT")
        assert (
            await client.mark_pending_with_lease(
                writer,
                row.id,
                token,
                delay_seconds=0,
                attempts_count=1,
                first_attempt_at=dt.datetime.now(dt.UTC),
                last_attempt_at=dt.datetime.now(dt.UTC),
            )
            is True
        )
    async with engine.connect() as claim:
        retry = (await client.fetch(claim, ["integration.outbox"], limit=1, lease_ttl_seconds=120))[
            0
        ]
    retry_token = retry.acquired_token
    assert retry_token is not None
    async with engine.connect() as writer_base:
        writer = await writer_base.execution_options(isolation_level="AUTOCOMMIT")
        await client.delete_with_lease(
            writer,
            retry.id,
            retry_token,
            dlq_payload={"failure_reason": "integration", "last_exception": "boom"},
        )
    async with engine.connect() as connection:
        assert (
            await connection.execute(
                text("SELECT count(*) FROM outbox_messages WHERE id=:id"), {"id": row_id}
            )
        ).scalar_one() == 0
        assert (
            await connection.execute(
                text("SELECT count(*) FROM outbox_dlq WHERE original_id=:id"),
                {"id": row_id},
            )
        ).scalar_one() == 1
        dlq = (
            await connection.execute(
                text(
                    "SELECT original_id, payload, headers, deliveries_count, timer_id, "
                    "failure_reason, last_exception "
                    "FROM outbox_dlq WHERE original_id=:id"
                ),
                {"id": row_id},
            )
        ).one()
        assert dlq.original_id == row_id
        assert dlq.payload == b"{}"
        assert dlq.headers == {"content-type": "application/json"}
        assert dlq.deliveries_count == 2
        assert dlq.timer_id == "integration.timer"
        assert dlq.failure_reason == "integration"
        assert dlq.last_exception == "boom"


@pytest.mark.integration
async def test_stale_lease_token_does_not_create_dlq(engine: AsyncEngine) -> None:
    client = OutboxClient(engine, outbox_messages, dlq_table=outbox_dlq)
    row_id = await _insert_message(engine)
    async with engine.connect() as claim:
        row = (await client.fetch(claim, ["integration.outbox"], limit=1, lease_ttl_seconds=120))[0]
    async with engine.connect() as writer_base:
        writer = await writer_base.execution_options(isolation_level="AUTOCOMMIT")
        assert (
            await client.delete_with_lease(
                writer,
                row.id,
                uuid4(),
                dlq_payload={"failure_reason": "stale", "last_exception": "old"},
            )
            is False
        )
    async with engine.connect() as connection:
        assert (
            await connection.execute(
                text("SELECT count(*) FROM outbox_messages WHERE id=:id"), {"id": row_id}
            )
        ).scalar_one() == 1
        assert (
            await connection.execute(
                text("SELECT count(*) FROM outbox_dlq WHERE original_id=:id"), {"id": row_id}
            )
        ).scalar_one() == 0
