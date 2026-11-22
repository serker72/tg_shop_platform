"""Durable ``bot.commands`` consumer for the worker transport boundary.

Runtime ownership remains in task-10/11.  This module only validates envelopes,
performs durable duplicate suppression, and delegates to an explicit adapter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast
from uuid import uuid4

import structlog
from faststream import AckPolicy
from faststream.exceptions import NackMessage, RejectMessage
from faststream.rabbit import QueueType, RabbitBroker, RabbitQueue
from redis.asyncio import Redis

from app.modules.bot_manager.commands import BOT_COMMAND_EXCHANGE, BotCommand

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


class RedisCommandDeduplicator:
    def __init__(self, redis: Redis, *, ttl_seconds: int = DEDUPE_TTL_SECONDS) -> None:
        self.redis = redis
        self.ttl_seconds = ttl_seconds
        self._release_script = self.redis.register_script(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        self._claim_script = self.redis.register_script(
            "if redis.call('exists', KEYS[2]) == 1 then return 0 end "
            "return redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) and 1 or 0"
        )
        self._complete_script = self.redis.register_script(
            "if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end "
            "redis.call('set', KEYS[2], '1', 'EX', ARGV[2]) "
            "redis.call('del', KEYS[1]) return 1"
        )

    async def claim(self, key: str) -> str | None:
        owner = str(uuid4())
        claimed = await self._claim_script(
            keys=[_DEDUPE_PREFIX + key, _COMPLETED_PREFIX + key],
            args=[owner, self.ttl_seconds],
        )
        return owner if claimed else None

    async def complete(self, key: str, owner: str) -> bool:
        result = await self._complete_script(
            keys=[_DEDUPE_PREFIX + key, _COMPLETED_PREFIX + key],
            args=[owner, COMPLETED_TTL_SECONDS],
        )
        return bool(result)

    async def release(self, key: str, owner: str) -> None:
        await self._release_script(keys=[_DEDUPE_PREFIX + key], args=[owner])


class InMemoryCommandDeduplicator:
    """Small deterministic adapter for unit tests; production uses Redis."""

    def __init__(self, *, ttl_seconds: int = DEDUPE_TTL_SECONDS) -> None:
        self._keys: dict[str, str] = {}
        self._completed: set[str] = set()
        self.ttl_seconds = ttl_seconds

    async def claim(self, key: str) -> str | None:
        if key in self._completed or key in self._keys:
            return None
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


async def dispatch_bot_command(
    command: BotCommand,
    adapter: BotRuntimeCommandAdapter,
    *,
    timeout_seconds: float = DISPATCH_TIMEOUT_SECONDS,
) -> None:
    handlers: dict[str, Callable[[BotCommand], Awaitable[None]]] = {
        "START_BOT": adapter.start,
        "STOP_BOT": adapter.stop,
        "RESTART_BOT": adapter.restart,
    }
    try:
        await asyncio.wait_for(handlers[command.kind](command), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise TransientDispatchTimeout(
            f"bot command dispatch exceeded {timeout_seconds:g}s"
        ) from exc


def register_bot_command_consumer(
    broker: RabbitBroker,
    adapter: BotRuntimeCommandAdapter,
    deduplicator: RedisCommandDeduplicator | InMemoryCommandDeduplicator,
    *,
    dispatch_timeout_seconds: float | None = None,
) -> Callable[[dict[str, Any]], Awaitable[None]]:
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

    async def handle(body: dict[str, Any], **kwargs: Any) -> None:
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
            raise NackMessage(requeue=True) from exc
        if owner is None:
            _log.info("bot_command_duplicate", idempotency_key=key)
            return

        try:
            await dispatch_bot_command(
                command,
                adapter,
                timeout_seconds=(
                    _dispatch_timeout_for(deduplicator.ttl_seconds)
                    if dispatch_timeout_seconds is None
                    else dispatch_timeout_seconds
                ),
            )
        except asyncio.CancelledError:
            # Cancellation must reach FastStream unchanged. Best-effort release is
            # limited to this delivery's owner and must never mask cancellation.
            try:
                await deduplicator.release(key, owner)
            except Exception as release_exc:
                _log.warning("bot_command_dedupe_release_failed", error=str(release_exc))
            raise
        except Exception as exc:
            # Release only a claim owned by this delivery. The Redis release itself
            # is compare-and-delete, so a late delivery cannot delete a new owner.
            try:
                await deduplicator.release(key, owner)
            except Exception as release_exc:
                _log.warning("bot_command_dedupe_release_failed", error=str(release_exc))
                # Retrying is unsafe: the claim may still be active, so a redelivery
                # could be ACKed as a duplicate without dispatching the command.
                raise RejectMessage(requeue=False) from release_exc
            if is_transient_adapter_error(exc):
                raise NackMessage(requeue=True) from exc
            # Terminal and unknown failures are poison/DLQ candidates, never an
            # unconditional requeue.
            if isinstance(exc, (RejectMessage, NackMessage)):
                raise
            raise RejectMessage(requeue=False) from exc
        # Completion is an atomic owner check + durable marker + lease release.
        # If the process crashes before this point, redelivery is intentionally
        # at-least-once and may dispatch again; it cannot silently lose the command.
        try:
            completed = await deduplicator.complete(key, owner)
        except Exception as exc:
            # A failed settlement is retryable; release only our lease so the
            # broker can redeliver instead of parking the command for its TTL.
            try:
                await deduplicator.release(key, owner)
            except Exception as release_exc:
                raise RejectMessage(requeue=False) from release_exc
            raise NackMessage(requeue=True) from exc
        if not completed:
            raise NackMessage(requeue=True)
        _log.info("bot_command_done", command=command.kind, traceparent=kwargs.get("traceparent"))

    broker.subscriber(
        RabbitQueue(
            COMMAND_QUEUE,
            durable=True,
            arguments=cast(Any, COMMAND_QUEUE_ARGUMENTS),
        ),
        ack_policy=AckPolicy.REJECT_ON_ERROR,
    )(handle)
    return handle


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
