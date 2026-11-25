"""Durable ``bot.commands`` consumer for the worker transport boundary.

Runtime ownership remains in task-10/11.  This module only validates envelopes,
performs durable duplicate suppression, and delegates to an explicit adapter.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, Protocol, cast
from uuid import uuid4

import structlog
from faststream import AckPolicy
from faststream.exceptions import NackMessage, RejectMessage
from faststream.rabbit import QueueType, RabbitBroker, RabbitMessage, RabbitQueue
from redis.asyncio import Redis

from app.modules.bot_manager.commands import BOT_COMMAND_EXCHANGE, BOT_COMMAND_FANOUT, BotCommand

COMMAND_QUEUE = BOT_COMMAND_EXCHANGE
COMMAND_DLQ = "bot.commands.dlq"
COMMAND_QUEUE_ARGUMENTS: dict[str, Any] = {
    "x-dead-letter-exchange": "",
    "x-dead-letter-routing-key": COMMAND_DLQ,
}
_DEDUPE_PREFIX = "bc:i:"
_COMPLETED_PREFIX = "bc:c:"
# The in-flight lease is deliberately short; completed state survives lease expiry.
DEDUPE_TTL_SECONDS = 120
COMPLETED_TTL_SECONDS = 86_400
DISPATCH_TIMEOUT_SECONDS = 100
DISPATCH_TIMEOUT_MARGIN_SECONDS = 20
_log = structlog.get_logger(__name__)


class BotRuntimeCommandAdapter(Protocol):
    async def start(self, command: BotCommand) -> None: ...
    async def stop(self, command: BotCommand) -> None: ...
    async def restart(self, command: BotCommand) -> None: ...


class TransientAdapterError(Exception):
    """An adapter failure that is safe to retry through the transport."""


class TransientTransportError(Exception):
    """A transport/infrastructure failure that is safe to retry."""


class TransientDispatchTimeout(TransientAdapterError):
    """The adapter exceeded the bounded dispatch window and must be retried."""


def is_transient_adapter_error(exc: BaseException) -> bool:
    """Classify only explicitly supported transient failures.

    A generic ``RuntimeError`` is deliberately terminal: the worker must not
    create an unbounded Rabbit requeue loop for an unavailable runtime seam.
    """
    return isinstance(exc, (TransientAdapterError, TransientTransportError))


def _dispatch_timeout_for(ttl_seconds: int) -> float:
    timeout = min(DISPATCH_TIMEOUT_SECONDS, ttl_seconds - DISPATCH_TIMEOUT_MARGIN_SECONDS)
    if timeout <= 0:
        raise ValueError("dedupe TTL must exceed dispatch timeout margin")
    return timeout


class ClaimState(Enum):
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"


class RedisCommandDeduplicator:
    def __init__(self, redis: Redis, *, ttl_seconds: int = DEDUPE_TTL_SECONDS) -> None:
        self.redis = redis
        self.ttl_seconds = ttl_seconds
        self._release_script = self.redis.register_script(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        self._claim_script = self.redis.register_script(
            "if redis.call('exists', KEYS[2]) == 1 then return 2 end "
            "return redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) and 1 or 0"
        )
        self._renew_script = self.redis.register_script(
            "if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end "
            "return redis.call('expire', KEYS[1], ARGV[2])"
        )
        self._complete_script = self.redis.register_script(
            "if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end "
            "redis.call('set', KEYS[2], '1', 'EX', ARGV[2]) "
            "redis.call('del', KEYS[1]) return 1"
        )

    async def claim(self, key: str) -> str | ClaimState:
        owner = str(uuid4())
        claimed = await self._claim_script(
            keys=[_DEDUPE_PREFIX + key, _COMPLETED_PREFIX + key],
            args=[owner, self.ttl_seconds],
        )
        if claimed == 1:
            return owner
        return ClaimState.COMPLETED if claimed == 2 else ClaimState.IN_FLIGHT

    async def complete(self, key: str, owner: str) -> bool:
        result = await self._complete_script(
            keys=[_DEDUPE_PREFIX + key, _COMPLETED_PREFIX + key],
            args=[owner, COMPLETED_TTL_SECONDS],
        )
        return bool(result)

    async def release(self, key: str, owner: str) -> None:
        await self._release_script(keys=[_DEDUPE_PREFIX + key], args=[owner])

    async def renew(self, key: str, owner: str) -> bool:
        return bool(
            await self._renew_script(keys=[_DEDUPE_PREFIX + key], args=[owner, self.ttl_seconds])
        )


class InMemoryCommandDeduplicator:
    """Small deterministic adapter for unit tests; production uses Redis."""

    def __init__(self, *, ttl_seconds: int = DEDUPE_TTL_SECONDS) -> None:
        self._keys: dict[str, str] = {}
        self._completed: set[str] = set()
        self.ttl_seconds = ttl_seconds

    async def claim(self, key: str) -> str | ClaimState:
        if key in self._completed:
            return ClaimState.COMPLETED
        if key in self._keys:
            return ClaimState.IN_FLIGHT
        owner = str(uuid4())
        self._keys[key] = owner
        return owner

    async def complete(self, key: str, owner: str) -> bool:
        if self._keys.get(key) != owner:
            return False
        self._keys.pop(key, None)
        self._completed.add(key)
        return True

    async def release(self, key: str, owner: str) -> None:
        if self._keys.get(key) == owner:
            self._keys.pop(key, None)

    async def renew(self, key: str, owner: str) -> bool:
        return self._keys.get(key) == owner


async def _retry_delay() -> None:
    await asyncio.sleep(random.SystemRandom().uniform(0.5, 1.5))


async def _renew_claim(
    dedupe: RedisCommandDeduplicator | InMemoryCommandDeduplicator,
    key: str,
    owner: str,
    request_cancel: Callable[[], None],
    uncertain: asyncio.Event,
) -> None:
    interval = dedupe.ttl_seconds / 3
    while True:
        await asyncio.sleep(interval)
        try:
            async with asyncio.timeout(interval):
                retained = await dedupe.renew(key, owner)
        except Exception:
            retained = None
        if retained is not True:
            # Stop initiating effects on uncertain ownership. Keep trying to renew
            # through cleanup unless Redis confirms that this owner lost the lease.
            if not uncertain.is_set():
                uncertain.set()
                _log.warning("bot_command_claim_renewal_failed", idempotency_key=key)
                request_cancel()
            if retained is False:
                return


async def dispatch_bot_command(
    command: BotCommand,
    adapter: BotRuntimeCommandAdapter,
    *,
    timeout_seconds: float | None = DISPATCH_TIMEOUT_SECONDS,
) -> None:
    handlers: dict[str, Callable[[BotCommand], Awaitable[None]]] = {
        "START_BOT": adapter.start,
        "STOP_BOT": adapter.stop,
        "RESTART_BOT": adapter.restart,
    }
    try:
        if timeout_seconds is None:
            await handlers[command.kind](command)
        else:
            async with asyncio.timeout(timeout_seconds):
                await handlers[command.kind](command)
    except TimeoutError as exc:
        detail = (
            "bot command dispatch timed out"
            if timeout_seconds is None
            else f"bot command dispatch exceeded {timeout_seconds:g}s"
        )
        raise TransientDispatchTimeout(detail) from exc


def register_bot_command_consumer(
    broker: RabbitBroker,
    adapter: BotRuntimeCommandAdapter,
    deduplicator: RedisCommandDeduplicator | InMemoryCommandDeduplicator,
    *,
    dispatch_timeout_seconds: float | None = None,
) -> Callable[[dict[str, Any], RabbitMessage], Awaitable[None]]:
    """Register one durable competing consumer on the shared broker.

    Invalid payloads and adapter failures are raised so FastStream's
    ``REJECT_ON_ERROR`` applies its configured safe error semantics. Failed
    dispatches release the claim and can be retried; successful duplicates are acked.
    Trace headers are carried by the broker telemetry middleware and included in logs.
    """

    if dispatch_timeout_seconds is not None and not (
        0 < dispatch_timeout_seconds < _dispatch_timeout_for(deduplicator.ttl_seconds)
    ):
        raise ValueError("dispatch timeout must be strictly below dedupe TTL with margin")

    async def handle(body: dict[str, Any], message: RabbitMessage) -> None:
        try:
            command = BotCommand.model_validate(body)
        except Exception as exc:
            # Poison envelopes are terminal: reject without requeue and without UUID
            # generation. Only explicitly classified failures are requeueable.
            raise RejectMessage(requeue=False) from exc

        key = str(command.idempotency_key)
        try:
            owner = await deduplicator.claim(key)
        except Exception as exc:
            # Claim is part of the delivery boundary. Never lose the command when
            # Redis is unavailable or times out before an owner is acquired.
            await _retry_delay()
            raise NackMessage(requeue=True) from exc
        if owner is ClaimState.COMPLETED:
            _log.info("bot_command_duplicate", idempotency_key=key)
            return
        if owner is ClaimState.IN_FLIGHT:
            # Keep this delivery unacked while delaying to avoid a hot requeue loop.
            # On disconnect RabbitMQ redelivers it; after lease expiry it can claim.
            await _retry_delay()
            raise NackMessage(requeue=True)

        timeout = dispatch_timeout_seconds or _dispatch_timeout_for(deduplicator.ttl_seconds)
        deadline_expired = False
        cancel_requested = False

        def request_cancel() -> None:
            nonlocal cancel_requested
            if not cancel_requested and not processing.done():
                cancel_requested = True
                if not processing.cancelling():
                    processing.cancel()

        def on_deadline() -> None:
            nonlocal deadline_expired
            deadline_expired = True
            request_cancel()

        async def process() -> None:
            try:
                await dispatch_bot_command(command, adapter, timeout_seconds=None)
            finally:
                deadline.cancel()
            if cancel_requested:
                raise TransientTransportError("command dispatch interrupted")
            try:
                completed = await deduplicator.complete(key, owner)
            except Exception as exc:
                raise TransientTransportError("command settlement uncertain") from exc
            if not completed:
                raise TransientTransportError("command lease lost before settlement")

        processing = asyncio.create_task(process())
        uncertain = asyncio.Event()
        deadline = asyncio.get_running_loop().call_later(timeout, on_deadline)
        renewal = asyncio.create_task(
            _renew_claim(deduplicator, key, owner, request_cancel, uncertain)
        )

        async def release() -> None:
            try:
                await deduplicator.release(key, owner)
            except Exception as exc:
                _log.warning("bot_command_dedupe_release_failed", error=type(exc).__name__)

        async def finish_release() -> None:
            releasing = asyncio.create_task(release())
            cancelled = False
            while not releasing.done():
                try:
                    await asyncio.shield(releasing)
                except asyncio.CancelledError:
                    cancelled = True
            releasing.result()
            if cancelled:
                raise asyncio.CancelledError

        try:
            await asyncio.shield(processing)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            cancelled = current is not None and current.cancelling()
            request_cancel()
            # Repeated parent cancellation must not interrupt adapter cleanup.
            while not processing.done():
                try:
                    await asyncio.shield(processing)
                except asyncio.CancelledError:
                    cancelled = cancelled or bool(current and current.cancelling())
                except Exception:
                    break
            if not processing.cancelled():
                processing.exception()  # retrieve any cleanup failure
            await finish_release()
            if cancelled or not (uncertain.is_set() or deadline_expired):
                raise
            await _retry_delay()
            raise NackMessage(requeue=True) from None
        except Exception as exc:
            await finish_release()
            # Cancellation can surface as an adapter exception from cleanup.
            if (
                uncertain.is_set()
                or deadline_expired
                or is_transient_adapter_error(exc)
                or (isinstance(exc, NackMessage) and exc.extra_options.get("requeue"))
            ):
                await _retry_delay()
                raise NackMessage(requeue=True) from exc
            if isinstance(exc, (RejectMessage, NackMessage)):
                raise
            raise RejectMessage(requeue=False) from exc
        finally:
            deadline.cancel()
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
        _log.info(
            "bot_command_done", command=command.kind, traceparent=message.headers.get("traceparent")
        )

    broker.subscriber(
        _command_queue(),
        exchange=BOT_COMMAND_FANOUT,
        ack_policy=AckPolicy.REJECT_ON_ERROR,
    )(handle)
    return handle


def _command_queue() -> RabbitQueue:
    return RabbitQueue(COMMAND_QUEUE, durable=True, arguments=cast(Any, COMMAND_QUEUE_ARGUMENTS))


async def declare_bot_command_topology(broker: RabbitBroker) -> None:
    """Declare the same durable queue and binding used by the optional subscriber."""
    await declare_bot_command_dlq(broker)
    schema = _command_queue()
    queue = await broker.declare_queue(schema)
    exchange = await broker.declare_exchange(BOT_COMMAND_FANOUT)
    await queue.bind(exchange, routing_key=schema.routing(), arguments=schema.bind_arguments)


async def declare_bot_command_dlq(broker: RabbitBroker) -> None:
    """Declare the durable dead-letter target on the shared broker."""
    await broker.declare_queue(RabbitQueue(COMMAND_DLQ, queue_type=QueueType.CLASSIC, durable=True))


__all__ = [
    "BotRuntimeCommandAdapter",
    "TransientAdapterError",
    "TransientTransportError",
    "TransientDispatchTimeout",
    "DEDUPE_TTL_SECONDS",
    "DISPATCH_TIMEOUT_SECONDS",
    "is_transient_adapter_error",
    "COMMAND_QUEUE",
    "COMMAND_DLQ",
    "COMMAND_QUEUE_ARGUMENTS",
    "declare_bot_command_dlq",
    "InMemoryCommandDeduplicator",
    "RedisCommandDeduplicator",
    "dispatch_bot_command",
    "register_bot_command_consumer",
]
