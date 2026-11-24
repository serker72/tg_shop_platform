"""Shared transactional outbox and RabbitMQ relay.

The relay deliberately uses ``OutboxClient.fetch``/``delete_with_lease``/
``mark_pending_with_lease``.  ``OutboxPublisher`` is a producer handle and the
installed package explicitly does not support publisher decoration/chaining.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import structlog
from aio_pika.abc import FieldValue
from faststream.rabbit import RabbitBroker, RabbitExchange, RabbitQueue
from faststream_outbox import OutboxBroker, make_dlq_table, make_outbox_table
from faststream_outbox.message import OutboxInnerMessage
from faststream_outbox.retry import ExponentialRetry, RetryStrategyProto
from opentelemetry.context import Context, attach, detach
from opentelemetry.propagate import extract
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.shared_kernel.infrastructure.database import metadata

# The package owns the transport schema. Business identifiers and context belong in
# the serialized body/headers, never in extra required columns.
outbox_messages: Table = make_outbox_table(metadata, table_name="outbox_messages")
outbox_dlq: Table = make_dlq_table(metadata, table_name="outbox_dlq")
OUTBOX_QUEUE = "bot.commands"


def build_outbox_broker(engine: AsyncEngine) -> OutboxBroker:
    """Create the package broker using the application's shared transport table."""
    return OutboxBroker(engine, outbox_table=outbox_messages, dlq_table=outbox_dlq)


async def append_outbox(
    broker: OutboxBroker,
    session: AsyncSession,
    *,
    payload: Any,
    headers: dict[str, str],
    idempotency_key: str,
    queue: str = OUTBOX_QUEUE,
) -> int | None:
    """Append without committing; caller owns the surrounding SQL transaction."""
    return await broker.publish(
        payload,
        queue=queue,
        session=session,
        headers=headers,
        timer_id=idempotency_key,
        timer_conflict="ignore",
    )


@dataclass(frozen=True, slots=True)
class RelayConfig:
    """Bounded lease/poll policy for the shared relay."""

    # Worst case is 10 publishes x 10 seconds plus settlement overhead; 120 seconds
    # leaves a bounded margin while preserving at-least-once reclaim semantics.
    lease_ttl_seconds: float = 120.0
    batch_size: int = 10
    poll_interval_seconds: float = 0.5
    publish_timeout_seconds: float = 10.0
    lease_margin_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be greater than zero")
        if self.batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        if self.publish_timeout_seconds <= 0:
            raise ValueError("publish_timeout_seconds must be greater than zero")
        if self.lease_margin_seconds < 0:
            raise ValueError("lease_margin_seconds must not be negative")
        required = self.batch_size * self.publish_timeout_seconds + self.lease_margin_seconds
        if required >= self.lease_ttl_seconds:
            raise ValueError(
                "unsafe outbox lease bounds: "
                f"batch_size * publish_timeout_seconds + lease_margin_seconds ({required:g}) "
                f"must be less than lease_ttl_seconds ({self.lease_ttl_seconds:g})"
            )


class SharedOutboxRelay:
    """Relay shared outbox rows to the already configured RabbitBroker.

    Semantics are at-least-once: publish confirmation is required before the
    lease-scoped delete.  A crash or confirmation timeout can therefore produce
    a duplicate, and consumers must use the envelope idempotency key.  Failures
    use the package retry strategy and its ``mark_pending_with_lease`` API;
    exhaustion deletes through the package DLQ path when configured.  Lease
    expiry is reclaimed by the package ``fetch`` query (``FOR UPDATE SKIP
    LOCKED``), without custom columns or a second broker.
    """

    def __init__(
        self,
        *,
        outbox_broker: OutboxBroker,
        rabbit_broker: RabbitBroker,
        engine: AsyncEngine,
        queues: tuple[str, ...] = (OUTBOX_QUEUE,),
        exchanges: Mapping[str, RabbitExchange] | None = None,
        config: RelayConfig | None = None,
        retry_strategy: RetryStrategyProto | None = None,
    ) -> None:
        dlq_table = outbox_broker.config.broker_config.dlq_table
        if dlq_table is None:
            raise RuntimeError("SharedOutboxRelay requires the configured durable outbox_dlq table")
        self._outbox_broker = outbox_broker
        self._rabbit_broker = rabbit_broker
        self._engine = engine
        self._queues = queues
        self._exchanges = dict(exchanges or {})
        self._config = config or RelayConfig()
        self._retry = retry_strategy or ExponentialRetry(
            initial_delay_seconds=1.0, multiplier=2.0, max_delay_seconds=300.0, max_attempts=10
        )
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._log = structlog.get_logger(__name__)

    async def run_once(self) -> int:
        """Claim briefly, then publish and settle outside the claim transaction.

        ``OutboxClient.fetch`` owns its short claim transaction. Settlement uses a
        separate AUTOCOMMIT writer connection, as required by faststream-outbox 0.21.0;
        no application-level commit is added to the package contract.
        """
        processed = 0
        async with self._engine.connect() as claim_conn:
            rows = await self._outbox_broker.client.fetch(
                claim_conn,
                self._queues,
                limit=self._config.batch_size,
                lease_ttl_seconds=self._config.lease_ttl_seconds,
            )
        async with self._engine.connect() as writer_base:
            writer_conn = await writer_base.execution_options(isolation_level="AUTOCOMMIT")
            for row in rows:
                processed += 1
                # fetch claims rows and always supplies a lease token; keep the
                # invariant explicit for the package's Optional model field.
                token = row.acquired_token
                if token is None:
                    raise RuntimeError("outbox fetch returned an unleased row")
                try:
                    await self._publish(row)
                except Exception as exc:
                    row.retry_strategy = self._retry
                    row.last_exception = exc
                    await row.nack()
                    if row.pending_delay_seconds is not None:
                        first_attempt_at = row.first_attempt_at
                        last_attempt_at = row.last_attempt_at
                        if first_attempt_at is None or last_attempt_at is None:
                            raise RuntimeError("retry outcome has no attempt timestamps") from exc
                        await self._outbox_broker.client.mark_pending_with_lease(
                            writer_conn,
                            row.id,
                            token,
                            delay_seconds=row.pending_delay_seconds,
                            attempts_count=row.attempts_count,
                            first_attempt_at=first_attempt_at,
                            last_attempt_at=last_attempt_at,
                        )
                    else:
                        await self._delete_terminal(writer_conn, row, exc)
                else:
                    await self._outbox_broker.client.delete_with_lease(writer_conn, row.id, token)
        return processed

    async def _delete_terminal(
        self, conn: Any, row: OutboxInnerMessage, exc: BaseException
    ) -> None:
        """Use the package DLQ CTE only when the broker was configured with a DLQ table."""
        token = row.acquired_token
        if token is None:
            raise RuntimeError("terminal outcome has no lease token")
        dlq_table = self._outbox_broker.config.broker_config.dlq_table
        payload = (
            None
            if dlq_table is None
            else {
                "failure_reason": row.terminal_failure_reason or "retry_terminal",
                "last_exception": f"{type(exc).__name__}: {exc}",
            }
        )
        result = await self._outbox_broker.client.delete_with_lease(
            conn, row.id, token, dlq_payload=payload
        )
        if (
            result is False
            and payload is not None
            and not await self._verify_dlq_settlement(conn, row.id, payload)
        ):
            raise RuntimeError(
                "faststream-outbox DLQ settlement was not verified: "
                f"delete_with_lease returned {result!r}"
            )

    async def _verify_dlq_settlement(
        self, conn: Any, row_id: int, payload: Mapping[str, str | None]
    ) -> bool:
        """Verify the package CTE's durable outcome when PostgreSQL reports no rowcount.

        faststream-outbox 0.21.0 uses ``WITH deleted ... INSERT ... SELECT`` for DLQ
        settlement. PostgreSQL drivers may expose an unknown/zero rowcount for that CTE;
        the package statement remains atomic, so verification must inspect both package-owned
        tables rather than retrying or treating ``False`` as success.
        """
        dlq = self._outbox_broker.config.broker_config.dlq_table
        if dlq is None:
            return False
        dlq_row = (
            await conn.execute(
                select(outbox_dlq.c.id)
                .where(
                    outbox_dlq.c.original_id == row_id,
                    outbox_dlq.c.failure_reason == payload["failure_reason"],
                    outbox_dlq.c.last_exception == payload["last_exception"],
                )
                .limit(1)
            )
        ).first()
        remaining = (
            await conn.execute(
                select(outbox_messages.c.id).where(outbox_messages.c.id == row_id).limit(1)
            )
        ).first()
        return dlq_row is not None and remaining is None

    async def _publish(self, row: OutboxInnerMessage) -> None:
        # payload and headers are already package-encoded. Do not decode/re-encode:
        # this preserves content-type, routing headers and W3C traceparent exactly.
        exchange = self._exchanges.get(row.queue)
        routing = (
            {"exchange": exchange, "mandatory": True}
            if exchange
            else {"queue": RabbitQueue(row.queue, durable=True)}
        )
        carrier = dict(row.headers or {})
        headers: dict[str, FieldValue] = dict(carrier)
        # Use the committed producer context, not the relay task's ambient context.
        context_token = attach(extract(carrier, context=Context()))
        try:
            await self._rabbit_broker.publish(
                row.payload,
                **routing,
                headers=headers,
                message_id=str(row.id),
                persist=True,
                timeout=self._config.publish_timeout_seconds,
            )
        finally:
            detach(context_token)

    @property
    def task(self) -> asyncio.Task[None] | None:
        """Current lifecycle task for process supervisor registration."""
        return self._task

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run_forever(), name="shared-outbox-relay")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_forever(self) -> None:
        """Supervise transient DB/Rabbit failures without losing cancellation."""
        delay = self._config.poll_interval_seconds
        max_delay = max(delay, 30.0)
        while not self._stop.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.exception(
                    "shared_outbox_relay_iteration_failed", error=type(exc).__name__
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)
            else:
                delay = self._config.poll_interval_seconds
                if processed == 0:
                    await asyncio.sleep(delay)


__all__ = [
    "OUTBOX_QUEUE",
    "RelayConfig",
    "SharedOutboxRelay",
    "append_outbox",
    "build_outbox_broker",
    "outbox_dlq",
    "outbox_messages",
]
