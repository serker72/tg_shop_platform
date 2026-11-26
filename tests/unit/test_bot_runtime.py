from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import uuid4

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.methods import GetUpdates
from aiogram.types import Update
from aiogram.utils.token import TokenValidationError
from python_socks import ProxyError

from app.core.config import Settings
from app.core.observability.metrics import build_metrics
from app.modules.bot_manager.models import Bot
from app.modules.bot_manager.runtime import BotRunner, BotSupervisor, FinalStatusWriteError
from app.modules.bot_manager.sessions import build_bot_session

from ._aiogram_mock import MockedSession

pytestmark = pytest.mark.req("R-II.9", "R-II.14")
TEST_TOKEN = "42:test-token"  # noqa: S105 - synthetic aiogram token


async def _ignore_status(status: str, error: str | None) -> None:
    pass


async def test_runner_uses_injected_mocked_session_and_closes() -> None:
    session = MockedSession([])
    statuses: list[str] = []

    async def sink(status: str, error: str | None) -> None:
        statuses.append(status)

    runner = BotRunner(
        uuid4(), 42, uuid4(), TEST_TOKEN, None, None, Dispatcher(), sink, session=session
    )
    task = runner.start()
    try:
        await asyncio.wait_for(session.polling.wait(), timeout=2)
        await runner.close()
        await task
    finally:
        await runner.stop()
    assert session.closed
    assert statuses == ["starting", "running", "stopped"]


async def test_unauthorized_does_not_report_proxy_failure() -> None:
    method = GetUpdates(offset=None, timeout=30, allowed_updates=[])
    session = MockedSession(TelegramUnauthorizedError(method, "invalid token"))
    statuses: list[tuple[str, str | None]] = []
    failures: list[Exception] = []

    async def sink(status: str, error: str | None) -> None:
        statuses.append((status, error))

    async def on_failure(bot_id: Any, proxy_id: Any, error: Exception) -> None:
        failures.append(error)

    runner = BotRunner(
        uuid4(),
        42,
        uuid4(),
        TEST_TOKEN,
        uuid4(),
        "socks5://user:password@127.0.0.1:1",
        Dispatcher(),
        sink,
        on_proxy_failure=on_failure,
        session=session,
    )
    task = runner.start()
    await asyncio.wait_for(task, timeout=2)
    assert statuses == [("starting", None), ("invalid_token", "TelegramUnauthorizedError")]
    assert failures == []
    assert "password" not in repr(task.exception())


async def test_supervisor_isolates_unauthorized_runner() -> None:
    metrics = build_metrics()
    supervisor = BotSupervisor(metrics=metrics)
    dispatcher = Dispatcher()
    unauthorized_bot = Bot(id=uuid4(), telegram_bot_id=42, tenant_id=uuid4(), proxy_id=None)
    healthy_bot = Bot(id=uuid4(), telegram_bot_id=43, tenant_id=uuid4(), proxy_id=None)
    unauthorized_session = MockedSession(TelegramUnauthorizedError(GetUpdates(), "invalid token"))
    healthy_session = MockedSession([])
    allow_unauthorized_poll = asyncio.Event()
    healthy_running = asyncio.Event()
    allow_healthy_next_poll = asyncio.Event()
    unauthorized_statuses: list[tuple[str, str | None]] = []

    async def unauthorized_sink(status: str, error: str | None) -> None:
        unauthorized_statuses.append((status, error))
        if status == "starting":
            await allow_unauthorized_poll.wait()

    async def healthy_sink(status: str, error: str | None) -> None:
        if status == "running":
            healthy_running.set()
            await allow_healthy_next_poll.wait()

    try:
        unauthorized = await supervisor.start(
            unauthorized_bot,
            token=TEST_TOKEN,
            dispatcher=dispatcher,
            status_sink=unauthorized_sink,
            session=unauthorized_session,
        )
        await supervisor.start(
            healthy_bot,
            token="43:test-token",
            dispatcher=dispatcher,
            status_sink=healthy_sink,
            session=healthy_session,
        )
        await asyncio.wait_for(healthy_running.wait(), timeout=2)
        assert supervisor.running_ids() == {unauthorized_bot.id, healthy_bot.id}
        assert metrics.registry.get_sample_value("active_bots") == 2

        allow_unauthorized_poll.set()
        await asyncio.wait_for(unauthorized.completion, timeout=2)
        assert unauthorized_statuses == [
            ("starting", None),
            ("invalid_token", "TelegramUnauthorizedError"),
        ]
        assert unauthorized_session.closed

        # Only now release B to issue another getUpdates, after A has fully exited.
        allow_healthy_next_poll.set()
        await asyncio.wait_for(healthy_session.polling.wait(), timeout=2)
        assert not healthy_session.closed
        assert supervisor.running_ids() == {healthy_bot.id}
        assert metrics.registry.get_sample_value("active_bots") == 1
    finally:
        await supervisor.stop_all(timeout=2)


async def test_retry_after_is_respected_and_handler_error_does_not_stop_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    method = GetUpdates(offset=None, timeout=30, allowed_updates=[])
    update = Update(update_id=7)
    session = MockedSession(TelegramRetryAfter(method, "slow down", 3), [update], [])
    delays: list[float] = []
    calls = 0

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    async def feed(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("handler failed")

    dispatcher = Dispatcher()
    monkeypatch.setattr(dispatcher, "feed_update", feed)
    statuses: list[str] = []

    async def sink(status: str, error: str | None) -> None:
        statuses.append(status)

    metrics = build_metrics(component="bot_worker")
    runner = BotRunner(
        uuid4(),
        42,
        uuid4(),
        TEST_TOKEN,
        None,
        None,
        dispatcher,
        sink,
        session=session,
        metrics=metrics,
    )
    monkeypatch.setattr(runner, "_sleep", sleep)
    task = runner.start()
    await asyncio.wait_for(session.polling.wait(), timeout=2)
    await runner.close()
    await task
    assert delays == [3.0]
    assert calls == 1
    assert statuses.count("running") == 1
    assert metrics.registry.get_sample_value("app_errors_total", {"component": "bot_worker"}) == 1


async def test_real_local_socks5_is_classified_by_aiogram(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    credentials: list[tuple[bytes, bytes]] = []
    reported = asyncio.Event()

    async def socks(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            greeting = await reader.readexactly(2)
            await reader.readexactly(greeting[1])
            writer.write(b"\x05\x02")
            await writer.drain()
            _, user_len = await reader.readexactly(2)
            user = await reader.readexactly(user_len)
            password = await reader.readexactly((await reader.readexactly(1))[0])
            credentials.append((user, password))
            writer.write(b"\x01\x01")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(socks, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    proxy_url = f"socks5://test-login:test-auth-secret@127.0.0.1:{port}"
    session = build_bot_session(proxy_url)
    statuses: list[str] = []
    failures: list[Exception] = []

    async def sink(status: str, error: str | None) -> None:
        statuses.append(status)

    async def on_failure(bot_id: Any, proxy_id: Any, error: Exception) -> None:
        failures.append(error)

        reported.set()

    monkeypatch.setattr("app.modules.bot_manager.runtime.probe_handshake", lambda url: _noop())
    runner = BotRunner(
        uuid4(),
        42,
        uuid4(),
        TEST_TOKEN,
        uuid4(),
        proxy_url,
        Dispatcher(),
        sink,
        on_proxy_failure=on_failure,
        failure_threshold=1,
        session=session,
    )
    runner.start()
    try:
        await asyncio.wait_for(reported.wait(), timeout=2)
    finally:
        try:
            await runner.stop()
        finally:
            server.close()
            await server.wait_closed()
    assert credentials == [(b"test-login", b"test-auth-secret")]
    assert len(failures) == 1
    assert "authentication failure" in str(failures[0])
    assert not isinstance(failures[0], (TelegramUnauthorizedError, TelegramNetworkError))
    assert statuses == ["starting", "stopped"]
    assert "test-login" not in caplog.text
    assert "test-auth-secret" not in caplog.text


async def test_proxy_failure_callback_repeats_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    method = GetUpdates(offset=None, timeout=30, allowed_updates=[])
    session = MockedSession(
        TelegramNetworkError(method, "first"), [], TelegramNetworkError(method, "second")
    )
    failures: list[Exception] = []
    reported = asyncio.Event()

    async def callback(bot_id: Any, proxy_id: Any, error: Exception) -> None:
        failures.append(error)
        if len(failures) == 2:
            reported.set()

    async def no_sleep(seconds: float) -> None:
        pass

    runner = BotRunner(
        uuid4(),
        42,
        uuid4(),
        TEST_TOKEN,
        uuid4(),
        None,
        Dispatcher(),
        _ignore_status,
        on_proxy_failure=callback,
        failure_threshold=1,
        session=session,
    )
    monkeypatch.setattr(runner, "_sleep", no_sleep)
    task = runner.start()
    await asyncio.wait_for(reported.wait(), timeout=2)
    await runner.close()
    await task
    assert len(failures) == 2


@pytest.mark.parametrize("factory_fails", [True, False])
async def test_startup_session_error_sets_error_status(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, *, factory_fails: bool
) -> None:
    statuses: list[tuple[str, str | None]] = []
    session = MockedSession()
    secret = "startup-secret-sentinel"

    async def sink(status: str, error: str | None) -> None:
        statuses.append((status, error))

    def build_session(url: str | None) -> MockedSession:
        if factory_fails:
            raise ValueError(secret)
        return session

    monkeypatch.setattr("app.modules.bot_manager.runtime.build_bot_session", build_session)
    runner = BotRunner(uuid4(), 42, uuid4(), secret, None, None, Dispatcher(), sink)
    await asyncio.wait_for(runner.start(), timeout=2)
    expected = ValueError if factory_fails else TokenValidationError
    assert statuses == [("starting", None), ("error", expected.__name__)]
    assert session.closed is not factory_fails
    assert secret not in caplog.text


async def _noop() -> None:
    pass


@pytest.mark.parametrize("raw_auth_failure", [False, True])
async def test_proxy_failure_threshold_is_reported_once_and_counter_resets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, *, raw_auth_failure: bool
) -> None:
    method = GetUpdates(offset=None, timeout=30, allowed_updates=[])
    failure = (
        ProxyError("Username and password authentication failure: example-secret")
        if raw_auth_failure
        else TelegramNetworkError(method, "proxy connection failed")
    )
    session = MockedSession(failure, failure, [])
    proxy_id = uuid4()
    marked_ok: list[object] = []
    marked_fail: list[object] = []
    failures: list[Exception] = []

    class Health:
        async def mark_ok(self, value: Any) -> None:
            marked_ok.append(value)

        async def mark_fail(self, value: Any) -> None:
            marked_fail.append(value)

    probes: list[str] = []

    async def probe(url: str) -> None:
        probes.append(url)

    async def on_failure(bot_id: Any, value: Any, error: Exception) -> None:
        failures.append(error)

    async def no_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("app.modules.bot_manager.runtime.probe_handshake", probe)
    statuses: list[str] = []

    async def sink(status: str, error: str | None) -> None:
        statuses.append(status)

    runner = BotRunner(
        uuid4(),
        42,
        uuid4(),
        TEST_TOKEN,
        proxy_id,
        "socks5://user:password@127.0.0.1:1",
        Dispatcher(),
        sink,
        cast(Any, Health()),
        on_failure,
        failure_threshold=2,
        session=session,
    )
    monkeypatch.setattr(runner, "_sleep", no_sleep)
    runner.start()
    try:
        await asyncio.wait_for(session.polling.wait(), timeout=2)
    finally:
        await runner.stop()
    assert marked_fail == [proxy_id]
    assert probes == ["socks5://user:password@127.0.0.1:1"]
    assert failures == [failure]
    assert "example-secret" not in caplog.text
    assert marked_ok == [proxy_id]
    assert statuses == ["starting", "running", "stopped"]


async def test_supervisor_enforces_capacity_and_stop_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = build_metrics(component="capacity")
    monkeypatch.setenv("BOT_WORKER_MAX_BOTS", "1")
    monkeypatch.setenv("PROXY_FAILURE_THRESHOLD", "3")
    settings = Settings(_env_file=None)
    supervisor = BotSupervisor.from_settings(settings, metrics=metrics)
    first = Bot(id=uuid4(), telegram_bot_id=42, tenant_id=uuid4(), proxy_id=None)
    second = Bot(id=uuid4(), telegram_bot_id=43, tenant_id=uuid4(), proxy_id=None)
    first_session = MockedSession([])
    second_session = MockedSession([])
    dispatcher = Dispatcher()

    await supervisor.start(
        first,
        token=TEST_TOKEN,
        dispatcher=dispatcher,
        status_sink=_ignore_status,
        session=first_session,
    )
    with pytest.raises(RuntimeError, match="MAX_BOTS"):
        await supervisor.start(
            second,
            token=TEST_TOKEN,
            dispatcher=dispatcher,
            status_sink=_ignore_status,
            session=second_session,
        )
    assert metrics.registry.get_sample_value("active_bots") == 1
    assert supervisor.failure_threshold == 3
    await asyncio.wait_for(first_session.polling.wait(), timeout=2)
    await supervisor.stop(first.id, timeout=0.01)
    assert metrics.registry.get_sample_value("active_bots") == 0


async def test_supervisor_requires_explicit_decrypted_token_and_updates_metric() -> None:
    metrics = build_metrics(component="test")
    supervisor = BotSupervisor(max_bots=2, metrics=metrics, active_metric=metrics.active_bots)
    first = Bot(id=uuid4(), telegram_bot_id=42, tenant_id=uuid4(), proxy_id=None)
    second = Bot(id=uuid4(), telegram_bot_id=43, tenant_id=uuid4(), proxy_id=None)
    dispatcher = Dispatcher()

    def count() -> float | None:
        return metrics.registry.get_sample_value("active_bots")

    with pytest.raises(TypeError, match="decrypted token"):
        await supervisor.start(first, dispatcher=dispatcher, status_sink=_ignore_status)
    assert count() == 0

    try:
        sessions = (MockedSession([]), MockedSession([]))
        for bot, session in zip((first, second), sessions, strict=True):
            await supervisor.start(
                bot,
                token=TEST_TOKEN,
                dispatcher=dispatcher,
                status_sink=_ignore_status,
                session=session,
            )
            await asyncio.wait_for(session.polling.wait(), timeout=2)
        assert supervisor.running_ids() == {first.id, second.id}
        assert count() == 2
        await supervisor.stop(first.id)
        assert supervisor.running_ids() == {second.id}
        assert count() == 1
    finally:
        await supervisor.stop_all()
    assert count() == 0


class SlowCloseSession(MockedSession):
    def __init__(self) -> None:
        super().__init__([])
        self.closing = asyncio.Event()
        self.release = asyncio.Event()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.closing.set()
        await self.release.wait()
        await super().close()


async def test_stop_deadline_and_cancelled_waiter_retain_slot() -> None:
    metrics = build_metrics()
    supervisor = BotSupervisor(metrics=metrics)
    bot = Bot(id=uuid4(), telegram_bot_id=42, tenant_id=uuid4(), proxy_id=None)
    session = SlowCloseSession()
    dispatcher = Dispatcher()
    runner = await supervisor.start(
        bot, token=TEST_TOKEN, dispatcher=dispatcher, status_sink=_ignore_status, session=session
    )
    try:
        await asyncio.wait_for(session.polling.wait(), timeout=2)
        with pytest.raises(TimeoutError, match="still stopping"):
            await supervisor.stop(bot.id, timeout=0.01)
        assert session.closing.is_set()
        waiter = asyncio.create_task(supervisor.stop(bot.id))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert (
            await supervisor.start(
                bot, token=TEST_TOKEN, dispatcher=dispatcher, status_sink=_ignore_status
            )
            is runner
        )
        assert supervisor.running_ids() == {bot.id}
        assert metrics.registry.get_sample_value("active_bots") == 1
    finally:
        session.release.set()
        await supervisor.stop_all()
    assert session.close_calls == 1
    assert supervisor.running_ids() == set()
    assert metrics.registry.get_sample_value("active_bots") == 0


async def test_stop_all_preserves_later_starts() -> None:
    supervisor = BotSupervisor(metrics=build_metrics())
    first = Bot(id=uuid4(), telegram_bot_id=42, tenant_id=uuid4(), proxy_id=None)
    second = Bot(id=uuid4(), telegram_bot_id=43, tenant_id=uuid4(), proxy_id=None)
    slow = SlowCloseSession()
    dispatcher = Dispatcher()
    await supervisor.start(
        first, token=TEST_TOKEN, dispatcher=dispatcher, status_sink=_ignore_status, session=slow
    )
    stopping = asyncio.create_task(supervisor.stop_all())
    try:
        await asyncio.wait_for(slow.closing.wait(), timeout=2)
        await supervisor.start(
            second,
            token=TEST_TOKEN,
            dispatcher=dispatcher,
            status_sink=_ignore_status,
            session=MockedSession([]),
        )
        slow.release.set()
        await stopping
        assert supervisor.running_ids() == {second.id}
    finally:
        slow.release.set()
        await stopping
        await supervisor.stop_all()


async def test_cancel_before_run_still_closes_session() -> None:
    session = SlowCloseSession()
    runner = BotRunner(
        uuid4(), 42, uuid4(), TEST_TOKEN, None, None, Dispatcher(), _ignore_status, session=session
    )
    task = runner.start()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    session.release.set()
    await runner.stop()
    assert session.closed and session.close_calls == 1


async def test_cancel_run_during_polling_then_stop_finalizes_once() -> None:
    session = SlowCloseSession()
    session.release.set()
    statuses: list[tuple[str, str | None]] = []

    async def sink(status: str, error: str | None) -> None:
        statuses.append((status, error))

    runner = BotRunner(
        uuid4(), 42, uuid4(), TEST_TOKEN, None, None, Dispatcher(), sink, session=session
    )
    caller = asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(session.polling.wait(), timeout=2)
        caller.cancel()
        # Let cancellation finish before stop, which would otherwise set the stop flag itself.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=2)
    finally:
        await runner.stop(timeout=2)
        await asyncio.gather(caller, return_exceptions=True)

    assert statuses == [("starting", None), ("running", None), ("stopped", None)]
    assert session.closed and session.close_calls == 1


async def test_cancel_run_stops_polling_when_handler_swallows_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SlowCloseSession()
    unexpected_poll = TelegramUnauthorizedError(GetUpdates(), "unexpected second poll")
    session.responses.extend(([Update(update_id=1)], unexpected_poll))
    session.release.set()
    entered, cancelled, release_handler = asyncio.Event(), asyncio.Event(), asyncio.Event()
    statuses: list[str] = []

    async def feed(*args: object, **kwargs: object) -> None:
        entered.set()
        try:
            await release_handler.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release_handler.wait()

    async def sink(status: str, error: str | None) -> None:
        statuses.append(status)

    dispatcher = Dispatcher()
    monkeypatch.setattr(dispatcher, "feed_update", feed)
    runner = BotRunner(
        uuid4(), 42, uuid4(), TEST_TOKEN, None, None, dispatcher, sink, session=session
    )
    caller = asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        caller.cancel()
        await asyncio.wait_for(cancelled.wait(), timeout=2)
        assert session.close_calls == 0
        assert not runner.completion.done()
        release_handler.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=2)
        await asyncio.wait_for(runner.completion, timeout=2)
        assert list(session.responses) == [unexpected_poll]
        assert statuses == ["starting", "running", "stopped"]
        assert session.closed and session.close_calls == 1
    finally:
        release_handler.set()
        await runner.stop(timeout=2)
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.parametrize(
    ("status", "error_type"),
    [("invalid_token", TelegramUnauthorizedError), ("error", TokenValidationError)],
    ids=["unauthorized", "startup-error"],
)
async def test_stop_does_not_interrupt_terminal_status_write(
    status: str, error_type: type[Exception]
) -> None:
    unauthorized = TelegramUnauthorizedError(GetUpdates(), "invalid token")
    session = SlowCloseSession()
    session.responses.clear()
    session.responses.append(unauthorized)
    session.release.set()
    sink_blocked = asyncio.Event()
    release_sink = asyncio.Event()
    stop_entered = asyncio.Event()
    persisted: list[tuple[str, str | None]] = []

    async def sink(value: str, error: str | None) -> None:
        if value == status:
            sink_blocked.set()
            await release_sink.wait()
        persisted.append((value, error))

    token = TEST_TOKEN if status == "invalid_token" else "not-a-valid-token"
    runner = BotRunner(uuid4(), 42, uuid4(), token, None, None, Dispatcher(), sink, session=session)

    async def stop() -> None:
        stop_entered.set()
        await runner.stop(timeout=2)

    runner.start()
    stopping: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(sink_blocked.wait(), timeout=2)
        stopping = asyncio.create_task(stop())
        await asyncio.wait_for(stop_entered.wait(), timeout=2)
        assert not stopping.done()
        assert persisted == [("starting", None)]
    finally:
        release_sink.set()
        if stopping is None:
            await runner.stop(timeout=2)
        else:
            await stopping

    assert persisted == [("starting", None), (status, error_type.__name__)]
    assert session.closed and session.close_calls == 1


async def test_cancel_completion_waiter_preserves_live_polling_slot() -> None:
    metrics = build_metrics()
    supervisor = BotSupervisor(metrics=metrics, max_bots=1)
    bot = Bot(id=uuid4(), telegram_bot_id=42, tenant_id=uuid4(), proxy_id=None)
    session = MockedSession([])
    duplicate_session = MockedSession()
    dispatcher = Dispatcher()
    runner = await supervisor.start(
        bot, token=TEST_TOKEN, dispatcher=dispatcher, status_sink=_ignore_status, session=session
    )
    waiter_entered = asyncio.Event()

    async def wait_for_completion() -> None:
        waiter_entered.set()
        await runner.completion

    try:
        await asyncio.wait_for(session.polling.wait(), timeout=2)
        waiter = asyncio.create_task(wait_for_completion())
        await asyncio.wait_for(waiter_entered.wait(), timeout=2)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, timeout=2)

        duplicate = await supervisor.start(
            bot,
            token=TEST_TOKEN,
            dispatcher=dispatcher,
            status_sink=_ignore_status,
            session=duplicate_session,
        )
        assert duplicate is runner
        assert not runner.completion.done()
        assert supervisor.running_ids() == {bot.id}
        assert metrics.registry.get_sample_value("active_bots") == 1
        assert not duplicate_session.polling.is_set()
        assert not session.closed
    finally:
        await runner.stop(timeout=2)
        await supervisor.stop_all(timeout=2)
    assert session.closed
    assert not duplicate_session.polling.is_set()
    assert supervisor.running_ids() == set()
    assert metrics.registry.get_sample_value("active_bots") == 0


@pytest.mark.parametrize("use_proxy", [False, True])
async def test_polling_backoff_is_bounded_and_hook_errors_are_isolated(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, *, use_proxy: bool
) -> None:
    failure = TelegramNetworkError(GetUpdates(), "network failure")
    session = MockedSession(*([failure] * 12), [], failure, [])
    delays: list[float] = []
    statuses: list[str] = []
    reports: list[Exception] = []

    async def sink(status: str, error: str | None) -> None:
        statuses.append(status)
        if status != "stopped":
            raise RuntimeError("private-status-detail")

    async def callback(bot_id: Any, proxy_id: Any, exc: Exception) -> None:
        reports.append(exc)
        raise RuntimeError("private-callback-detail")

    async def probe(url: str) -> None:
        raise RuntimeError("private-probe-detail")

    class Health:
        async def mark_ok(self, proxy_id: Any) -> None:
            raise RuntimeError("private-health-detail")

        async def mark_fail(self, proxy_id: Any) -> None:
            raise RuntimeError("private-health-detail")

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("app.modules.bot_manager.runtime.random.uniform", lambda low, high: high)
    monkeypatch.setattr("app.modules.bot_manager.runtime.probe_handshake", probe)
    runner = BotRunner(
        uuid4(),
        42,
        uuid4(),
        TEST_TOKEN,
        uuid4() if use_proxy else None,
        "socks5://example.test:1" if use_proxy else None,
        Dispatcher(),
        sink,
        cast(Any, Health()),
        callback,
        session=session,
        failure_threshold=1,
    )
    monkeypatch.setattr(runner, "_sleep", sleep)
    runner.start()
    try:
        await asyncio.wait_for(session.polling.wait(), timeout=2)
    finally:
        await runner.stop()
    assert delays == [1, 2, 4, 8, 16, 32] + [60] * 6 + [1]
    assert statuses == ["starting", "running", "stopped"]
    assert reports == ([failure, failure] if use_proxy else [])
    assert "private-" not in caplog.text


async def test_run_cancellation_survives_final_status_write_failure() -> None:
    session = SlowCloseSession()
    writing, release_write, stop_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    statuses: list[str] = []

    async def sink(status: str, error: str | None) -> None:
        statuses.append(status)
        if status == "stopped":
            writing.set()
            await release_write.wait()
            raise ValueError("status write failed")

    metrics = build_metrics()
    supervisor = BotSupervisor(metrics=metrics, max_bots=1)
    bot = Bot(id=uuid4(), telegram_bot_id=42, tenant_id=uuid4(), proxy_id=None)
    runner = await supervisor.start(
        bot, token=TEST_TOKEN, dispatcher=Dispatcher(), status_sink=sink, session=session
    )

    async def stop() -> None:
        stop_entered.set()
        await supervisor.stop(bot.id, timeout=2)

    caller = asyncio.create_task(runner.run())
    completion = runner.completion
    stopping: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(session.polling.wait(), timeout=2)
        caller.cancel()
        await asyncio.wait_for(session.closing.wait(), timeout=2)
        assert not session.closed
        assert not completion.done()
        assert supervisor.running_ids() == {bot.id}
        assert metrics.registry.get_sample_value("active_bots") == 1

        session.release.set()
        await asyncio.wait_for(writing.wait(), timeout=2)
        stopping = asyncio.create_task(stop())
        await asyncio.wait_for(stop_entered.wait(), timeout=2)
        assert session.closed and session.close_calls == 1
        assert not completion.done() and not stopping.done()
        assert supervisor.running_ids() == {bot.id}
        assert metrics.registry.get_sample_value("active_bots") == 1
        release_write.set()
        # A deadline must not cancel the caller and manufacture the expected error.
        _, pending = await asyncio.wait((caller, completion, stopping), timeout=3)
        assert not pending, "Runner and cleanup waiters did not finish"
        with pytest.raises(asyncio.CancelledError):
            await caller
        with pytest.raises(FinalStatusWriteError, match="persist stopped"):
            await completion
        with pytest.raises(FinalStatusWriteError, match="persist stopped"):
            await stopping
    except BaseException:
        session.release.set()
        release_write.set()
        if not caller.done():
            caller.cancel()
        tasks = (caller, completion) if stopping is None else (caller, completion, stopping)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)
        raise
    assert statuses == ["starting", "running", "stopped"]
    assert session.closed and session.close_calls == 1
    assert not supervisor.running_ids()
    assert metrics.registry.get_sample_value("active_bots") == 0


@pytest.mark.parametrize("final_status", ["stopped", "invalid_token", "error"])
async def test_final_status_failure_reaches_completion_and_stop(
    final_status: str, caplog: pytest.LogCaptureFixture
) -> None:
    session = SlowCloseSession()
    session.release.set()
    if final_status == "invalid_token":
        session.responses.clear()
        session.responses.append(TelegramUnauthorizedError(GetUpdates(), "invalid token"))
    statuses: list[str] = []
    writing, release_write = asyncio.Event(), asyncio.Event()

    async def sink(status: str, error: str | None) -> None:
        statuses.append(status)
        if status == final_status:
            writing.set()
            await release_write.wait()
            raise ValueError("private-storage-detail")

    metrics = build_metrics()
    supervisor = BotSupervisor(metrics=metrics)
    bot = Bot(id=uuid4(), telegram_bot_id=42, tenant_id=uuid4(), proxy_id=None)
    runner = await supervisor.start(
        bot,
        token="bad" if final_status == "error" else TEST_TOKEN,
        dispatcher=Dispatcher(),
        status_sink=sink,
        session=session,
    )
    try:
        if final_status == "stopped":
            await asyncio.wait_for(session.polling.wait(), timeout=2)
            with pytest.raises(TimeoutError):
                await supervisor.stop(bot.id, timeout=0)
        await asyncio.wait_for(writing.wait(), timeout=2)
        with pytest.raises(TimeoutError):
            await supervisor.stop(bot.id, timeout=0)
        waiter = runner.completion
        abandoned = runner.completion
        abandoned.cancel()
        _ = runner.completion  # Deliberately discard a waiter without awaiting it.
        assert not waiter.done()
        assert supervisor.running_ids() == {bot.id}
    finally:
        release_write.set()
        with pytest.raises(FinalStatusWriteError, match=f"persist {final_status}") as failure:
            await supervisor.stop(bot.id, timeout=2)
    with pytest.raises(FinalStatusWriteError, match=f"persist {final_status}"):
        await waiter
    assert "private-storage-detail" not in str(failure.value)
    assert "private-storage-detail" not in caplog.text
    assert [s for s in statuses if s in {"stopped", "invalid_token", "error"}] == [final_status]
    assert session.closed and session.close_calls == 1
    assert not supervisor.running_ids()
    assert metrics.registry.get_sample_value("active_bots") == 0


@pytest.mark.parametrize(
    ("final_status", "write_fails"),
    [("stopped", False), ("stopped", True), ("invalid_token", True), ("error", True)],
)
async def test_cancelled_session_close_preserves_final_status(
    final_status: str, *, write_fails: bool
) -> None:
    class CancelledCloseSession(SlowCloseSession):
        async def close(self) -> None:
            self.close_calls += 1
            self.closing.set()
            await self.release.wait()
            raise asyncio.CancelledError

    session = CancelledCloseSession()
    if final_status == "invalid_token":
        session.responses.clear()
        session.responses.append(TelegramUnauthorizedError(GetUpdates(), "invalid token"))
    statuses: list[str] = []

    async def sink(status: str, error: str | None) -> None:
        statuses.append(status)
        if status == final_status and write_fails:
            raise ValueError("status write failed")

    metrics = build_metrics()
    supervisor = BotSupervisor(metrics=metrics)
    bot = Bot(id=uuid4(), telegram_bot_id=42, tenant_id=uuid4(), proxy_id=None)
    runner = await supervisor.start(
        bot,
        token="bad" if final_status == "error" else TEST_TOKEN,
        dispatcher=Dispatcher(),
        status_sink=sink,
        session=session,
    )
    waiter = runner.completion
    try:
        if final_status == "stopped":
            await asyncio.wait_for(session.polling.wait(), timeout=2)
            with pytest.raises(TimeoutError):
                await supervisor.stop(bot.id, timeout=0)
        await asyncio.wait_for(session.closing.wait(), timeout=2)
        assert not waiter.done()
        assert supervisor.running_ids() == {bot.id}
        assert metrics.registry.get_sample_value("active_bots") == 1
        assert (final_status in statuses) == (final_status != "stopped")
    finally:
        session.release.set()

    error_type = FinalStatusWriteError if write_fails else RuntimeError
    message = f"persist {final_status}" if write_fails else "cleanup was cancelled"
    with pytest.raises(error_type, match=message):
        await supervisor.stop(bot.id, timeout=2)
    with pytest.raises(error_type, match=message):
        await asyncio.wait_for(waiter, timeout=2)
    assert statuses == (
        ["starting", "running", "stopped"]
        if final_status == "stopped"
        else ["starting", final_status]
    )
    assert session.close_calls == 1
    assert not supervisor.running_ids()
    assert metrics.registry.get_sample_value("active_bots") == 0


async def test_stop_all_reports_final_status_failure_after_stopping_other_runner() -> None:
    metrics = build_metrics()
    supervisor = BotSupervisor(metrics=metrics)
    finished: list[str] = []

    async def failing_sink(status: str, error: str | None) -> None:
        if status == "stopped":
            raise ValueError("private-storage-detail")

    async def healthy_sink(status: str, error: str | None) -> None:
        if status == "stopped":
            finished.append(status)

    sessions = [MockedSession([]), MockedSession([])]
    for session, sink in zip(sessions, (failing_sink, healthy_sink), strict=True):
        bot = Bot(id=uuid4(), telegram_bot_id=42, tenant_id=uuid4(), proxy_id=None)
        await supervisor.start(
            bot, token=TEST_TOKEN, dispatcher=Dispatcher(), status_sink=sink, session=session
        )
        await asyncio.wait_for(session.polling.wait(), timeout=2)

    with pytest.raises(ExceptionGroup) as failure:
        await supervisor.stop_all(timeout=2)
    assert len(failure.value.exceptions) == 1
    assert isinstance(failure.value.exceptions[0], FinalStatusWriteError)
    assert finished == ["stopped"]
    assert all(session.closed for session in sessions)
    assert not supervisor.running_ids()
    assert metrics.registry.get_sample_value("active_bots") == 0
