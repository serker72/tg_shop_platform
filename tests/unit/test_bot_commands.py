"""Contract tests for the Master Bot -> worker command envelope."""

import asyncio
from collections.abc import Callable
from contextvars import Context
from datetime import UTC, datetime
from functools import partial
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from faststream.exceptions import NackMessage, RejectMessage
from faststream.rabbit import RabbitBroker, RabbitMessage
from pydantic import ValidationError

from app.entrypoints.bot_worker.commands import (
    DEDUPE_TTL_SECONDS,
    DISPATCH_TIMEOUT_SECONDS,
    ClaimState,
    InMemoryCommandDeduplicator,
    TransientAdapterError,
    TransientDispatchTimeout,
    TransientTransportError,
    dispatch_bot_command,
    is_transient_adapter_error,
)
from app.modules.bot_manager.commands import BOT_COMMAND_EXCHANGE, BotCommand

pytestmark = pytest.mark.req("R-II.8")


async def test_start_fanout_carries_traceparent() -> None:
    from faststream.rabbit import TestRabbitBroker
    from opentelemetry.context import attach, detach
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context

    from app.core.config import Settings
    from app.core.observability.broker import build_rabbit_broker
    from app.modules.bot_manager.commands import BOT_COMMAND_FANOUT
    from app.modules.bot_manager.publisher import BotCommandPublisher

    broker = build_rabbit_broker(Settings(_env_file=None))
    seen = []

    @broker.subscriber("bot-trace-test", exchange=BOT_COMMAND_FANOUT)
    async def receive(body: dict[str, Any], message: RabbitMessage) -> None:
        seen.append((BotCommand.model_validate(body), message.headers["traceparent"]))

    command = BotCommand.model_validate(command_payload())
    trace_id = 1234
    token = attach(
        set_span_in_context(
            NonRecordingSpan(
                SpanContext(
                    trace_id=trace_id,
                    span_id=4321,
                    is_remote=False,
                    trace_flags=TraceFlags(1),
                )
            )
        )
    )
    try:
        async with TestRabbitBroker(broker):
            await BotCommandPublisher(broker).publish(command)
    finally:
        detach(token)
    assert len(seen) == 1
    assert seen[0][0] == command
    assert seen[0][1].split("-")[1] == f"{trace_id:032x}"


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


def rabbit_message() -> RabbitMessage:
    return Mock(spec=RabbitMessage, headers={})


def command_payload() -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "kind": "START_BOT",
        "bot_id": str(uuid4()),
        "issued_at": datetime.now(UTC).isoformat(),
        "issued_by": 1,
    }


async def test_registered_consumer_dispatches_published_command_with_header_trace() -> None:
    from logging import Logger

    from faststream.rabbit import TestRabbitBroker
    from structlog.testing import capture_logs

    from app.entrypoints.bot_worker.commands import register_bot_command_consumer
    from app.modules.bot_manager.commands import BOT_COMMAND_FANOUT

    # Keep FastStream from configuring the process-wide faststream.access.rabbit logger.
    broker = RabbitBroker(logger=Logger(__name__))
    payload = command_payload()
    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    adapter = AsyncMock(spec=RuntimeAdapter)
    register_bot_command_consumer(broker, adapter, InMemoryCommandDeduplicator())

    with capture_logs() as logs:
        async with TestRabbitBroker(broker) as test_broker:
            await test_broker.publish(
                payload, exchange=BOT_COMMAND_FANOUT, headers={"traceparent": traceparent}
            )

    adapter.start.assert_awaited_once_with(BotCommand.model_validate(payload))
    adapter.stop.assert_not_awaited()
    adapter.restart.assert_not_awaited()
    assert [
        (event["command"], event["traceparent"])
        for event in logs
        if event["event"] == "bot_command_done"
    ] == [("START_BOT", traceparent)]


@pytest.mark.asyncio
async def test_dispatch_transient_terminal_and_crash_release_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        release = AsyncMock(wraps=dedupe.release)
        monkeypatch.setattr(dedupe, "release", release)
        handle = register_bot_command_consumer(
            cast(RabbitBroker, FakeBroker()), FailingAdapter(error), dedupe
        )
        payload = command_payload()
        with pytest.raises(expected):
            await handle(payload, rabbit_message())
        key = payload["idempotency_key"]
        release.assert_awaited_once()
        release_call = release.await_args
        assert release_call is not None
        released_key, old_owner = release_call.args
        assert released_key == key
        new_owner = await dedupe.claim(key)
        assert isinstance(new_owner, str) and new_owner != old_owner


@pytest.mark.asyncio
async def test_transient_dispatch_with_release_failure_is_requeued() -> None:
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
    with pytest.raises(NackMessage) as caught:
        await handle(command_payload(), rabbit_message())
    assert caught.value.extra_options == {"requeue": True}


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
        await handle(command_payload(), rabbit_message())
    assert dedupe.release_calls == 1


@pytest.mark.asyncio
async def test_redis_claim_failure_is_requeued_without_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from app.entrypoints.bot_worker import commands

    delay = AsyncMock()
    monkeypatch.setattr(commands, "_retry_delay", delay)
    register_bot_command_consumer = commands.register_bot_command_consumer

    class BrokenDedupe(InMemoryCommandDeduplicator):
        async def claim(self, key: str) -> str | ClaimState:
            raise ConnectionError("redis down")

        async def release(self, key: str, owner: str) -> None:
            pytest.fail("release must not run without an owned claim")

    handle = register_bot_command_consumer(
        cast(RabbitBroker, FakeBroker()), RuntimeAdapter(), BrokenDedupe()
    )
    with pytest.raises(NackMessage):
        await handle(command_payload(), rabbit_message())


@pytest.mark.parametrize("stored", [False, True])
async def test_uncertain_completion_with_release_failure(
    monkeypatch: pytest.MonkeyPatch, stored: bool
) -> None:
    from unittest.mock import AsyncMock

    from app.entrypoints.bot_worker import commands

    delay = AsyncMock()
    monkeypatch.setattr(commands, "_retry_delay", delay)

    class BrokenSettlement(InMemoryCommandDeduplicator):
        async def complete(self, key: str, owner: str) -> bool:
            if stored:
                await super().complete(key, owner)
            raise ConnectionError("completion reply lost")

        async def release(self, key: str, owner: str) -> None:
            raise ConnectionError("release reply lost")

    dedupe, adapter = BrokenSettlement(), RuntimeAdapter()
    handle = commands.register_bot_command_consumer(
        cast(RabbitBroker, FakeBroker()), adapter, dedupe
    )
    payload = command_payload()
    with pytest.raises(NackMessage):
        await handle(payload, rabbit_message())
    if stored:
        await handle(payload, rabbit_message())
        assert delay.await_count == 1
    else:
        with pytest.raises(NackMessage):
            await handle(payload, rabbit_message())
        assert delay.await_count == 2
    assert len(adapter.calls) == 1


@pytest.mark.parametrize("trigger", ["deadline", "renewal", "shutdown", "deadline_shutdown"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_claim_renews_during_cancel_cleanup(
    monkeypatch: pytest.MonkeyPatch, trigger: str, cleanup_fails: bool
) -> None:
    from unittest.mock import AsyncMock

    from app.entrypoints.bot_worker import commands

    backoff = AsyncMock()
    monkeypatch.setattr(commands, "_retry_delay", backoff)
    monkeypatch.setattr(commands, "DISPATCH_TIMEOUT_MARGIN_SECONDS", 0)
    started, cleanup, finish, renewed = (asyncio.Event() for _ in range(4))
    loop = asyncio.get_running_loop()
    call_later = loop.call_later
    deadlines: list[Callable[[], object]] = []

    def capture_deadline(
        delay: float, callback: Callable[..., object], *args: object, context: Context | None = None
    ) -> asyncio.TimerHandle:
        if getattr(callback, "__name__", "") == "on_deadline":
            deadlines.append(callback)
            return call_later(60, callback, *args, context=context)
        return call_later(delay, callback, *args, context=context)

    monkeypatch.setattr(loop, "call_later", capture_deadline)

    class Renewing(InMemoryCommandDeduplicator):
        async def renew(self, key: str, owner: str) -> bool:
            await started.wait()
            if cleanup.is_set():
                renewed.set()
            elif trigger == "renewal":
                raise ConnectionError("renewal reply lost")
            return await super().renew(key, owner)

    class SlowCleanup(RuntimeAdapter):
        async def start(self, command: BotCommand) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await finish.wait()
                if cleanup_fails:
                    raise RuntimeError("cleanup failed")

    dedupe = Renewing(ttl_seconds=1)
    release = AsyncMock(wraps=dedupe.release)
    complete = AsyncMock(wraps=dedupe.complete)
    monkeypatch.setattr(dedupe, "release", release)
    monkeypatch.setattr(dedupe, "complete", complete)
    handle = commands.register_bot_command_consumer(
        cast(RabbitBroker, FakeBroker()), SlowCleanup(), dedupe
    )
    payload = command_payload()
    task = asyncio.ensure_future(handle(payload, rabbit_message()))
    key = payload["idempotency_key"]
    try:
        await asyncio.wait_for(started.wait(), 2)
        if trigger in ("deadline", "deadline_shutdown"):
            deadlines[0]()
        elif trigger == "shutdown":
            task.cancel()
        await asyncio.wait_for(cleanup.wait(), 2)
        if trigger == "deadline_shutdown":
            task.cancel()
        elif trigger == "shutdown":
            deadlines[0]()
        await asyncio.wait_for(renewed.wait(), 2)
        assert not task.done()
        release.assert_not_awaited()
        assert await dedupe.claim(key) is ClaimState.IN_FLIGHT
    finally:
        finish.set()
    if "shutdown" in trigger:
        with pytest.raises(asyncio.CancelledError):
            await task
        backoff.assert_not_awaited()
    else:
        with pytest.raises(NackMessage) as error:
            await task
        assert error.value.extra_options == {"requeue": True}
        backoff.assert_awaited_once()
    release.assert_awaited_once()
    assert release.await_args is not None
    assert release.await_args.args[0] == key
    complete.assert_not_awaited()
    assert isinstance(await dedupe.claim(key), str)


@pytest.mark.asyncio
async def test_adapter_timeout_without_deadline_requeues_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.entrypoints.bot_worker import commands

    delay = AsyncMock()
    monkeypatch.setattr(commands, "_retry_delay", delay)

    class TimeoutAdapter(RuntimeAdapter):
        async def start(self, command: BotCommand) -> None:
            raise TimeoutError("adapter timeout")

    dedupe = InMemoryCommandDeduplicator()
    handle = commands.register_bot_command_consumer(
        cast(RabbitBroker, FakeBroker()), TimeoutAdapter(), dedupe
    )
    payload = command_payload()
    with pytest.raises(NackMessage) as error:
        await handle(payload, rabbit_message())
    assert error.value.extra_options == {"requeue": True}
    delay.assert_awaited_once()
    assert await dedupe.claim(payload["idempotency_key"]) is not None
    # Completion must not be recorded; the released owner can be claimed again.
    assert await dedupe.claim(payload["idempotency_key"]) is ClaimState.IN_FLIGHT


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
    assert isinstance(owner, str)
    assert await dedupe.claim("key") is ClaimState.IN_FLIGHT
    await dedupe.release("key", owner)
    assert await dedupe.claim("key") is not None


@pytest.mark.asyncio
async def test_completed_duplicate_is_acked_without_adapter_after_inflight_expiry() -> None:
    from app.entrypoints.bot_worker.commands import register_bot_command_consumer

    dedupe = InMemoryCommandDeduplicator(ttl_seconds=30)
    adapter = RuntimeAdapter()
    handle = register_bot_command_consumer(cast(RabbitBroker, FakeBroker()), adapter, dedupe)
    payload = command_payload()

    key = payload["idempotency_key"]
    owner = await dedupe.claim(key)
    assert isinstance(owner, str)
    with pytest.raises(NackMessage) as error:
        await handle(payload, rabbit_message())
    assert error.value.extra_options == {"requeue": True}
    assert adapter.calls == []
    await dedupe.release(key, owner)
    await handle(payload, rabbit_message())
    assert await dedupe.claim(key) is ClaimState.COMPLETED
    # The in-flight claim is gone, while the durable completed marker remains.
    assert len(adapter.calls) == 1
    await handle(payload, rabbit_message())
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_consumer_timeout_with_default_deadline_requeues_and_releases_without_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.entrypoints.bot_worker import commands

    backoff = AsyncMock()
    monkeypatch.setattr(commands, "_retry_delay", backoff)

    class TimeoutAdapter(RuntimeAdapter):
        async def start(self, command: BotCommand) -> None:
            raise TimeoutError("adapter timed out")

    dedupe = InMemoryCommandDeduplicator()
    complete = AsyncMock(wraps=dedupe.complete)
    release = AsyncMock(wraps=dedupe.release)
    monkeypatch.setattr(dedupe, "complete", complete)
    monkeypatch.setattr(dedupe, "release", release)
    handle = commands.register_bot_command_consumer(
        cast(RabbitBroker, FakeBroker()), TimeoutAdapter(), dedupe
    )
    payload = command_payload()
    with pytest.raises(NackMessage) as error:
        await handle(payload, rabbit_message())
    assert error.value.extra_options == {"requeue": True}
    assert backoff.await_count == 1
    release.assert_awaited_once()
    assert release.await_args is not None
    assert release.await_args.args[0] == payload["idempotency_key"]
    assert isinstance(release.await_args.args[1], str)
    complete.assert_not_awaited()
    assert isinstance(error.value.__cause__, TransientDispatchTimeout)
    assert isinstance(error.value.__cause__.__cause__, TimeoutError)


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
async def test_timeout_requeues_and_releases_owner_without_duplicate_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.entrypoints.bot_worker.commands import register_bot_command_consumer

    dispatch_timeout = 5.0
    loop = asyncio.get_running_loop()
    call_later = loop.call_later
    expire_dispatch: Callable[[], None] | None = None

    def capture_deadline(
        delay: float, callback: Callable[..., None], *args: Any, context: Context | None = None
    ) -> asyncio.TimerHandle:
        nonlocal expire_dispatch
        timer = call_later(delay, callback, *args, context=context)
        if delay == dispatch_timeout:
            expire_dispatch = partial(callback, *args)
        return timer

    monkeypatch.setattr(loop, "call_later", capture_deadline)

    class HangingAdapter(RuntimeAdapter):
        async def start(self, command: BotCommand) -> None:
            await super().start(command)
            # Trigger the consumer's deadline only once the adapter has entered.
            assert expire_dispatch is not None
            loop.call_soon(expire_dispatch)
            try:
                async with asyncio.timeout(1.0):
                    await asyncio.Event().wait()
            except TimeoutError:
                pytest.fail("Consumer deadline did not cancel the adapter dispatch")

    dedupe = InMemoryCommandDeduplicator()
    release = AsyncMock(wraps=dedupe.release)
    monkeypatch.setattr(dedupe, "release", release)
    adapter = HangingAdapter()
    handle = register_bot_command_consumer(
        cast(RabbitBroker, FakeBroker()), adapter, dedupe, dispatch_timeout_seconds=dispatch_timeout
    )
    payload = command_payload()
    with pytest.raises(NackMessage) as caught:
        await handle(payload, rabbit_message())
    assert caught.value.extra_options == {"requeue": True}
    key = payload["idempotency_key"]
    release.assert_awaited_once()
    release_call = release.await_args
    assert release_call is not None
    released_key, old_owner = release_call.args
    assert released_key == key
    new_owner = await dedupe.claim(key)
    assert isinstance(new_owner, str) and new_owner != old_owner
    assert adapter.calls == ["START_BOT"]
