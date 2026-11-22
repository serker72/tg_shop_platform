"""Unit contract tests for the shared transactional outbox relay."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from faststream.rabbit import RabbitBroker
from faststream_outbox import OutboxBroker
from sqlalchemy.ext.asyncio import AsyncEngine

from app.shared_kernel.infrastructure.outbox import RelayConfig, SharedOutboxRelay, append_outbox


class FakeConnection:
    def __init__(self) -> None:
        self.autocommit = False

    async def execution_options(self, *, isolation_level: str) -> FakeConnection:
        assert isolation_level == "AUTOCOMMIT"
        self.autocommit = True
        return self


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> FakeBegin:
        return FakeBegin(self.connection)

    def begin(self) -> FakeBegin:
        return FakeBegin(self.connection)


class FakeBegin:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeRow:
    def __init__(self, *, row_id: int = 7, lease_id: str | None = "lease") -> None:
        self.id = row_id
        self.queue = "bot.commands"
        self.payload = {"command": "sync"}
        self.headers = {"traceparent": "00-abc-def-01", "content-type": "application/json"}
        self.acquired_token = lease_id
        self.retry_strategy = None
        self.last_exception = None
        self.pending_delay_seconds: float | None = None
        self.attempts_count = 1
        self.first_attempt_at = "first"
        self.last_attempt_at = "last"
        self.terminal_failure_reason: str | None = None
        self.nack = AsyncMock(side_effect=self._nack)

    async def _nack(self) -> None:
        if (
            self.last_exception is not None
            and self.pending_delay_seconds is None
            and self.terminal_failure_reason is None
        ):
            self.pending_delay_seconds = 2.0


class FakeClient:
    def __init__(self, rows: list[FakeRow]) -> None:
        self.rows = rows
        self.fetch = AsyncMock(return_value=rows)
        self.mark_pending_with_lease = AsyncMock()
        self.delete_with_lease = AsyncMock()


@pytest.fixture
def relay_parts() -> tuple[FakeClient, AsyncMock, SharedOutboxRelay]:
    row = FakeRow()
    client = FakeClient([row])
    outbox = SimpleNamespace(
        client=client,
        config=SimpleNamespace(broker_config=SimpleNamespace(dlq_table="outbox_dlq")),
    )
    rabbit = SimpleNamespace(publish=AsyncMock())
    relay = SharedOutboxRelay(
        outbox_broker=cast(OutboxBroker, outbox),
        rabbit_broker=cast(RabbitBroker, rabbit),
        engine=cast(AsyncEngine, FakeEngine(FakeConnection())),
        config=RelayConfig(lease_ttl_seconds=30, batch_size=3, publish_timeout_seconds=4),
    )
    return client, rabbit.publish, relay


def test_relay_requires_configured_dlq_table() -> None:
    outbox = SimpleNamespace(
        client=SimpleNamespace(),
        config=SimpleNamespace(broker_config=SimpleNamespace(dlq_table=None)),
    )
    with pytest.raises(RuntimeError, match="outbox_dlq"):
        SharedOutboxRelay(
            outbox_broker=cast(OutboxBroker, outbox),
            rabbit_broker=cast(RabbitBroker, SimpleNamespace()),
            engine=cast(AsyncEngine, FakeEngine(FakeConnection())),
        )


def test_default_lease_bounds_serial_publish_batch() -> None:
    config = RelayConfig()
    assert (
        config.batch_size * config.publish_timeout_seconds + config.lease_margin_seconds
        < config.lease_ttl_seconds
    )


def test_lease_bounds_reject_unsafe_overrides() -> None:
    with pytest.raises(ValueError, match="unsafe outbox lease bounds"):
        RelayConfig(lease_ttl_seconds=30, batch_size=3, publish_timeout_seconds=7)


@pytest.mark.asyncio
async def test_run_once_publishes_before_lease_scoped_delete(
    relay_parts: tuple[FakeClient, AsyncMock, SharedOutboxRelay],
) -> None:
    client, publish, relay = relay_parts

    assert await relay.run_once() == 1

    client.fetch.assert_awaited_once()
    fetch_call = client.fetch.await_args
    assert fetch_call is not None
    assert fetch_call.kwargs["lease_ttl_seconds"] == 30
    publish.assert_awaited_once()
    publish_call = publish.await_args
    delete_call = client.delete_with_lease.await_args
    assert publish_call is not None
    assert delete_call is not None
    assert publish_call.kwargs["headers"]["traceparent"] == "00-abc-def-01"
    assert publish_call.kwargs["message_id"] == "7"
    assert delete_call.args[1:] == (7, "lease")
    assert cast(FakeConnection, delete_call.args[0]).autocommit is True


@pytest.mark.asyncio
async def test_publish_failure_marks_pending_with_same_lease_and_retry_metadata(
    relay_parts: tuple[FakeClient, AsyncMock, SharedOutboxRelay],
) -> None:
    client, publish, relay = relay_parts
    publish.side_effect = ConnectionError("rabbit outage")

    assert await relay.run_once() == 1

    client.mark_pending_with_lease.assert_awaited_once()
    mark_call = client.mark_pending_with_lease.await_args
    assert mark_call is not None
    kwargs = mark_call.kwargs
    assert kwargs["delay_seconds"] == 2.0
    assert kwargs["attempts_count"] == 1
    client.delete_with_lease.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_failure_deletes_with_dlq_payload() -> None:
    row = FakeRow()
    row.pending_delay_seconds = None
    row.terminal_failure_reason = "attempts_exhausted"
    client = FakeClient([row])
    outbox = SimpleNamespace(
        client=client,
        config=SimpleNamespace(broker_config=SimpleNamespace(dlq_table="outbox_dlq")),
    )
    rabbit = SimpleNamespace(publish=AsyncMock(side_effect=RuntimeError("bad message")))
    relay = SharedOutboxRelay(
        outbox_broker=cast(OutboxBroker, outbox),
        rabbit_broker=cast(RabbitBroker, rabbit),
        engine=cast(AsyncEngine, FakeEngine(FakeConnection())),
    )

    await relay.run_once()

    client.delete_with_lease.assert_awaited_once()
    delete_call = client.delete_with_lease.await_args
    assert delete_call is not None
    assert delete_call.kwargs["dlq_payload"] == {
        "failure_reason": "attempts_exhausted",
        "last_exception": "RuntimeError: bad message",
    }


@pytest.mark.asyncio
async def test_unleased_row_is_rejected_before_publish(
    relay_parts: tuple[FakeClient, AsyncMock, SharedOutboxRelay],
) -> None:
    client, publish, relay = relay_parts
    client.rows[0].acquired_token = None

    with pytest.raises(RuntimeError, match="unleased"):
        await relay.run_once()
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_append_passes_idempotency_key_and_headers_without_commit() -> None:
    broker = SimpleNamespace(publish=AsyncMock(return_value=11))
    session = object()

    result = await append_outbox(
        cast(OutboxBroker, broker),
        cast(Any, session),
        payload={"kind": "bot_state"},
        headers={"traceparent": "00-trace-span-01"},
        idempotency_key="same-command",
    )

    assert result == 11
    broker.publish.assert_awaited_once_with(
        {"kind": "bot_state"},
        queue="bot.commands",
        session=session,
        headers={"traceparent": "00-trace-span-01"},
        timer_id="same-command",
        timer_conflict="ignore",
    )


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_cancels_worker(
    relay_parts: tuple[FakeClient, AsyncMock, SharedOutboxRelay],
) -> None:
    _, _, relay = relay_parts

    async def blocked() -> None:
        await asyncio.sleep(60)

    relay.run_forever = AsyncMock(side_effect=blocked)  # type: ignore[method-assign]

    await relay.start()
    await asyncio.sleep(0)
    first = relay._task
    await relay.start()
    assert relay._task is first
    await relay.stop()
    assert relay._task is None
    relay.run_forever.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_preserves_payload_headers_and_timeout(
    relay_parts: tuple[FakeClient, AsyncMock, SharedOutboxRelay],
) -> None:
    _, publish, relay = relay_parts
    await relay.run_once()
    publish_call = publish.await_args
    assert publish_call is not None
    kwargs = publish_call.kwargs
    assert kwargs["headers"] == {"traceparent": "00-abc-def-01", "content-type": "application/json"}
    assert kwargs["persist"] is True
    assert kwargs["timeout"] == 4
    assert kwargs["queue"].name == "bot.commands"


@pytest.mark.asyncio
async def test_empty_batch_does_not_publish_or_delete(
    relay_parts: tuple[FakeClient, AsyncMock, SharedOutboxRelay],
) -> None:
    client, publish, relay = relay_parts
    client.fetch.return_value = []

    assert await relay.run_once() == 0
    publish.assert_not_awaited()
    client.delete_with_lease.assert_not_awaited()
