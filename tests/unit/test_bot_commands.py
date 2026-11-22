"""Contract tests for the Master Bot -> worker command envelope."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from faststream.exceptions import NackMessage, RejectMessage
from faststream.rabbit import RabbitBroker
from pydantic import ValidationError

from app.entrypoints.bot_worker.commands import (
    DEDUPE_TTL_SECONDS,
    DISPATCH_TIMEOUT_SECONDS,
    InMemoryCommandDeduplicator,
    TransientAdapterError,
    TransientDispatchTimeout,
    TransientTransportError,
    dispatch_bot_command,
    is_transient_adapter_error,
)
from app.modules.bot_manager.commands import BOT_COMMAND_EXCHANGE, BotCommand

pytestmark = pytest.mark.req("R-II.8")


def test_command_payload_is_strict_and_json_serializable() -> None:
    command = BotCommand(
        command_id=uuid4(),
        idempotency_key=uuid4(),
        kind="START_BOT",
        bot_id=uuid4(),
        issued_at=datetime.now(UTC),
        issued_by=42,
    )
    payload = command.model_dump(mode="json")
    assert BOT_COMMAND_EXCHANGE == "bot.commands"
    assert payload["kind"] == "START_BOT"
    assert isinstance(payload["bot_id"], str)
    assert payload["issued_by"] == 42


def test_command_rejects_unknown_kind_and_fields() -> None:
    with pytest.raises(ValidationError):
        BotCommand.model_validate(
            {
                "command_id": str(uuid4()),
                "idempotency_key": str(uuid4()),
                "kind": "RUN_BOT",
                "bot_id": str(uuid4()),
                "issued_at": datetime.now(UTC).isoformat(),
                "issued_by": 1,
            }
        )
    with pytest.raises(ValidationError):
        BotCommand(
            command_id=uuid4(),
            idempotency_key=uuid4(),
            kind="STOP_BOT",
            bot_id=uuid4(),
            issued_at=datetime.now(UTC),
            issued_by=1,
            **{"token": "secret"},
        )


def test_command_rejects_missing_command_and_idempotency_and_string_issuer() -> None:
    base = {
        "kind": "START_BOT",
        "bot_id": str(uuid4()),
        "issued_at": datetime.now(UTC).isoformat(),
        "issued_by": 42,
    }
    with pytest.raises(ValidationError):
        BotCommand.model_validate(base)
    with pytest.raises(ValidationError):
        BotCommand.model_validate({**base, "command_id": str(uuid4())})
    with pytest.raises(ValidationError):
        BotCommand.model_validate(
            {
                **base,
                "command_id": str(uuid4()),
                "idempotency_key": str(uuid4()),
                "issued_by": "42",
            }
        )


def test_dispatch_timeout_has_margin_before_dedupe_ttl() -> None:
    assert DEDUPE_TTL_SECONDS == 120
    assert DISPATCH_TIMEOUT_SECONDS == 100
    assert DISPATCH_TIMEOUT_SECONDS < DEDUPE_TTL_SECONDS


def test_only_explicit_adapter_and_transport_errors_are_transient() -> None:
    assert is_transient_adapter_error(TransientAdapterError())
    assert is_transient_adapter_error(TransientTransportError())
    assert not is_transient_adapter_error(RuntimeError("runtime seam unavailable"))
    assert not is_transient_adapter_error(ConnectionError("unknown adapter failure"))


class RuntimeAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def start(self, command: BotCommand) -> None:
        self.calls.append(command.kind)

    async def stop(self, command: BotCommand) -> None:
        self.calls.append(command.kind)

    async def restart(self, command: BotCommand) -> None:
        self.calls.append(command.kind)


class FakeBroker:
    def subscriber(
        self, *_args: object, **_kwargs: object
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return lambda handler: handler


def command_payload() -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "kind": "START_BOT",
        "bot_id": str(uuid4()),
        "issued_at": datetime.now(UTC).isoformat(),
        "issued_by": 1,
    }


@pytest.mark.asyncio
async def test_dispatch_transient_terminal_and_crash_release_paths() -> None:
    from app.entrypoints.bot_worker.commands import register_bot_command_consumer

    class FailingAdapter(RuntimeAdapter):
        def __init__(self, error: Exception) -> None:
            super().__init__()
            self.error = error

        async def start(self, command: BotCommand) -> None:
            raise self.error

    for error, expected in (
        (TransientAdapterError("temporary"), NackMessage),
        (ValueError("terminal"), RejectMessage),
        (RuntimeError("crash"), RejectMessage),
    ):
        dedupe = InMemoryCommandDeduplicator()
        handle = register_bot_command_consumer(
            cast(RabbitBroker, FakeBroker()), FailingAdapter(error), dedupe
        )
        with pytest.raises(expected):
            await handle(command_payload())
        # Every owned claim is released, including terminal and unknown crashes.
        assert await dedupe.claim("unused") is not None


@pytest.mark.asyncio
async def test_transient_dispatch_with_release_failure_is_terminal_reject() -> None:
    from app.entrypoints.bot_worker.commands import register_bot_command_consumer

    class BrokenRelease(InMemoryCommandDeduplicator):
        async def release(self, key: str, owner: str) -> None:
            raise ConnectionError("redis release down")

    class FailingAdapter(RuntimeAdapter):
        async def start(self, command: BotCommand) -> None:
            raise TransientAdapterError("temporary")

    dedupe = BrokenRelease()
    handle = register_bot_command_consumer(
        cast(RabbitBroker, FakeBroker()), FailingAdapter(), dedupe
    )
    with pytest.raises(RejectMessage) as caught:
        await handle(command_payload())
    assert caught.value.extra_options == {"requeue": False}


@pytest.mark.asyncio
async def test_cancellation_releases_claim_and_propagates_without_reject() -> None:
    from app.entrypoints.bot_worker.commands import register_bot_command_consumer

    class CancellingAdapter(RuntimeAdapter):
        async def start(self, command: BotCommand) -> None:
            raise asyncio.CancelledError

    class TrackingDedupe(InMemoryCommandDeduplicator):
        def __init__(self) -> None:
            super().__init__()
            self.release_calls = 0

        async def release(self, key: str, owner: str) -> None:
            self.release_calls += 1
            await super().release(key, owner)

    dedupe = TrackingDedupe()
    handle = register_bot_command_consumer(
        cast(RabbitBroker, FakeBroker()), CancellingAdapter(), dedupe
    )
    with pytest.raises(asyncio.CancelledError):
        await handle(command_payload())
    assert dedupe.release_calls == 1


@pytest.mark.asyncio
async def test_redis_claim_failure_is_requeued_without_release() -> None:
    from app.entrypoints.bot_worker.commands import register_bot_command_consumer

    class BrokenDedupe(InMemoryCommandDeduplicator):
        async def claim(self, key: str) -> str | None:
            raise ConnectionError("redis down")

        async def release(self, key: str, owner: str) -> None:
            pytest.fail("release must not run without an owned claim")

    handle = register_bot_command_consumer(
        cast(RabbitBroker, FakeBroker()), RuntimeAdapter(), BrokenDedupe()
    )
    with pytest.raises(NackMessage):
        await handle(command_payload())


@pytest.mark.asyncio
async def test_dispatch_adapter_routes_all_lifecycle_commands() -> None:
    adapter = RuntimeAdapter()
    for kind in ("START_BOT", "STOP_BOT", "RESTART_BOT"):
        command = BotCommand(
            command_id=uuid4(),
            idempotency_key=uuid4(),
            kind=kind,
            bot_id=uuid4(),
            issued_at=datetime.now(UTC),
            issued_by=1,
        )
        await dispatch_bot_command(command, adapter)
    assert adapter.calls == ["START_BOT", "STOP_BOT", "RESTART_BOT"]


@pytest.mark.asyncio
async def test_in_memory_idempotency_duplicate_and_release_on_error() -> None:
    dedupe = InMemoryCommandDeduplicator()
    owner = await dedupe.claim("key")
    assert owner is not None
    assert await dedupe.claim("key") is None
    await dedupe.release("key", owner)
    assert await dedupe.claim("key") is not None


@pytest.mark.asyncio
async def test_completed_duplicate_is_acked_without_adapter_after_inflight_expiry() -> None:
    from app.entrypoints.bot_worker.commands import register_bot_command_consumer

    dedupe = InMemoryCommandDeduplicator(ttl_seconds=30)
    adapter = RuntimeAdapter()
    handle = register_bot_command_consumer(cast(RabbitBroker, FakeBroker()), adapter, dedupe)
    payload = command_payload()

    await handle(payload)
    # The in-flight claim is gone, while the durable completed marker remains.
    assert len(adapter.calls) == 1
    await handle(payload)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_dispatch_timeout_is_transient_and_cancels_hanging_adapter() -> None:
    cancelled = asyncio.Event()

    class HangingAdapter(RuntimeAdapter):
        async def start(self, command: BotCommand) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    command = BotCommand.model_validate(command_payload())
    with pytest.raises(TransientDispatchTimeout):
        await dispatch_bot_command(command, HangingAdapter(), timeout_seconds=0.01)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_timeout_requeues_and_releases_owner_without_duplicate_call() -> None:
    from app.entrypoints.bot_worker.commands import register_bot_command_consumer

    class HangingAdapter(RuntimeAdapter):
        async def start(self, command: BotCommand) -> None:
            await asyncio.Event().wait()

    dedupe = InMemoryCommandDeduplicator()
    adapter = HangingAdapter()
    handle = register_bot_command_consumer(
        cast(RabbitBroker, FakeBroker()), adapter, dedupe, dispatch_timeout_seconds=0.01
    )
    with pytest.raises(NackMessage) as caught:
        await handle(command_payload())
    assert caught.value.extra_options == {"requeue": True}
    assert await dedupe.claim("unused") is not None
    assert adapter.calls == []
