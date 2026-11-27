"""Shared outbox migration compatibility against the isolated PostgreSQL stack."""

from __future__ import annotations

import asyncio
import datetime as dt
from contextlib import suppress
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aio_pika.exceptions import ChannelNotFoundEntity
from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange, RabbitMessage, RabbitQueue
from faststream_outbox.client import OutboxClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.core.observability.broker import build_rabbit_broker
from app.shared_kernel.infrastructure.outbox import (
    SharedOutboxRelay,
    append_outbox,
    build_outbox_broker,
    outbox_dlq,
    outbox_messages,
)
from tests.integration._guards import guarded_rabbit_broker, guarded_rabbit_url

pytestmark = pytest.mark.integration


async def test_worker_retries_unroutable_commands_without_adapter(
    engine: AsyncEngine, settings: Settings, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:

    import aio_pika
    from dishka import AsyncContainer
    from faststream_outbox import OutboxBroker
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.observability.health import HeartbeatRegistry
    from app.core.observability.metrics import build_metrics
    from app.core.process import ServiceContext
    from app.entrypoints.bot_worker import commands, scheduler
    from app.modules.bot_manager import tasks

    test_settings = settings
    url = guarded_rabbit_url(test_settings)

    def build_scheduler_broker(config: Settings) -> RabbitBroker:
        return guarded_rabbit_broker(build_rabbit_broker(config), test_settings)

    monkeypatch.setattr(scheduler, "build_rabbit_broker", build_scheduler_broker)
    name = f"test.worker.{uuid4().hex}"
    scheduler_queue = f"{name}.proxy.rehabilitate"
    monkeypatch.setattr(tasks, "task_queue", lambda _: scheduler_queue)
    dlq = f"{name}.dlq"
    exchange = RabbitExchange(name, type=ExchangeType.FANOUT, durable=True)
    monkeypatch.setattr(commands, "COMMAND_QUEUE", name)
    monkeypatch.setattr(commands, "COMMAND_DLQ", dlq)
    monkeypatch.setattr(
        commands,
        "COMMAND_QUEUE_ARGUMENTS",
        {
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": dlq,
        },
    )
    monkeypatch.setattr(commands, "BOT_COMMAND_FANOUT", exchange)
    monkeypatch.setattr(scheduler, "BOT_COMMAND_FANOUT", exchange)
    monkeypatch.setattr(scheduler, "BOT_COMMAND_EXCHANGE", name)
    # Isolate the test relay from other outbox rows while exercising production wiring.
    monkeypatch.setattr(
        scheduler, "SharedOutboxRelay", lambda **kw: SharedOutboxRelay(**kw, queues=(name,))
    )
    # The UUID queue isolates delivery, not the scheduler's shared Redis lock.
    settings = settings.model_copy(
        update={
            "project_environment": "prod",
            "scheduler": settings.scheduler.model_copy(update={"scheduler_enabled": False}),
        }
    )
    outbox = build_outbox_broker(engine)

    dependencies: dict[object, object] = {
        Redis: redis_client,
        AsyncEngine: engine,
        OutboxBroker: outbox,
        async_sessionmaker[AsyncSession]: async_sessionmaker(engine),
    }
    container = AsyncMock(spec=AsyncContainer)
    container.get.side_effect = dependencies.__getitem__

    readiness = HeartbeatRegistry()
    ctx = ServiceContext(
        name="bot_worker",
        settings=settings,
        container=container,
        metrics=build_metrics(component="bot_worker"),
        readiness_registry=readiness,
    )
    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    async with await aio_pika.connect(url) as connection:
        channel = await connection.channel()
        try:
            async with scheduler.scheduler_component(ctx):
                subscriber_queue = await channel.declare_queue(scheduler_queue, passive=True)
                assert subscriber_queue.declaration_result.consumer_count == 1
                assert not ctx.settings.scheduler.scheduler_enabled
                assert not readiness.readiness[scheduler.BOT_RUNTIME_READINESS].ready
                async with AsyncSession(engine) as session:
                    await append_outbox(
                        outbox,
                        session,
                        payload={"kind": "START_BOT"},
                        headers={"traceparent": traceparent},
                        idempotency_key=uuid4().hex,
                        queue=name,
                    )
                    await session.commit()
                async with asyncio.timeout(5):
                    while True:
                        async with engine.connect() as conn:
                            attempts = await conn.scalar(
                                select(outbox_messages.c.deliveries_count).where(
                                    outbox_messages.c.queue == name
                                )
                            )
                        if attempts and attempts > 0:
                            break
                        await asyncio.sleep(0.05)
                dead = await channel.declare_queue(dlq, passive=True)
                assert dead.declaration_result.message_count == 0
        finally:
            # Rabbit can close a channel if a resource was never declared during startup.
            # Give each deletion its own channel and always clean the SQL row as well.
            try:
                for queue in (scheduler_queue, dlq):
                    with suppress(ChannelNotFoundEntity):
                        async with connection.channel() as cleanup:
                            await cleanup.queue_delete(queue)
                with suppress(ChannelNotFoundEntity):
                    async with connection.channel() as cleanup:
                        await cleanup.exchange_delete(name)
            finally:
                async with engine.begin() as conn:
                    await conn.execute(
                        outbox_messages.delete().where(outbox_messages.c.queue == name)
                    )


async def test_shared_outbox_commit_relay_rabbit_trace_and_precommit_visibility(
    engine: AsyncEngine,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.observability import broker as broker_module

    guarded_rabbit_url(settings)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(broker_module, "get_tracer_provider", lambda: provider)
    queue_name = f"test.outbox.{uuid4().hex}"
    exchange = RabbitExchange(queue_name, type=ExchangeType.FANOUT, auto_delete=True)
    broker = guarded_rabbit_broker(build_rabbit_broker(settings), settings)
    received: asyncio.Future[tuple[dict[str, str], str]] = (
        asyncio.get_running_loop().create_future()
    )

    @broker.subscriber(RabbitQueue(queue_name, auto_delete=True), exchange=exchange)
    async def consume(body: dict[str, str], message: RabbitMessage) -> None:
        if not received.done():
            received.set_result((body, str(message.headers["traceparent"])))

    outbox = build_outbox_broker(engine)
    relay = SharedOutboxRelay(
        outbox_broker=outbox,
        rabbit_broker=broker,
        engine=engine,
        queues=(queue_name,),
        exchanges={queue_name: exchange},
    )
    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    async with broker:
        await broker.start()
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await append_outbox(
                    outbox,
                    session,
                    payload={"kind": "START_BOT"},
                    headers={"traceparent": traceparent},
                    idempotency_key=uuid4().hex,
                    queue=queue_name,
                )
                assert await relay.run_once() == 0
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(received), 0.2)
                await session.commit()
            assert await relay.run_once() == 1
            body, header = await asyncio.wait_for(received, 5)
            assert body == {"kind": "START_BOT"}
            assert header.split("-")[1] == traceparent.split("-")[1]
            assert any(
                span.parent is not None
                and span.parent.span_id == int(traceparent.split("-")[2], 16)
                and span.context is not None
                and span.context.trace_id == int(traceparent.split("-")[1], 16)
                for span in exporter.get_finished_spans()
            )
        finally:
            async with engine.begin() as conn:
                await conn.execute(
                    outbox_messages.delete().where(outbox_messages.c.queue == queue_name)
                )


async def test_shared_outbox_unroutable_fanout_keeps_row_for_retry(
    engine: AsyncEngine, settings: Settings
) -> None:
    guarded_rabbit_url(settings)
    name = f"test.unroutable.{uuid4().hex}"
    exchange = RabbitExchange(name, type=ExchangeType.FANOUT, auto_delete=True)
    broker = guarded_rabbit_broker(build_rabbit_broker(settings), settings)
    outbox = build_outbox_broker(engine)
    relay = SharedOutboxRelay(
        outbox_broker=outbox,
        rabbit_broker=broker,
        engine=engine,
        queues=(name,),
        exchanges={name: exchange},
    )
    async with broker:
        declared = await broker.declare_exchange(exchange)
        try:
            async with AsyncSession(engine) as session:
                await append_outbox(
                    outbox,
                    session,
                    payload={"kind": "START_BOT"},
                    headers={},
                    idempotency_key=uuid4().hex,
                    queue=name,
                )
                await session.commit()
            assert await relay.run_once() == 1
            async with engine.connect() as conn:
                attempts = await conn.scalar(
                    select(outbox_messages.c.deliveries_count).where(
                        outbox_messages.c.queue == name
                    )
                )
            assert attempts == 1
        finally:
            async with engine.begin() as conn:
                await conn.execute(outbox_messages.delete().where(outbox_messages.c.queue == name))
            await declared.delete()


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
