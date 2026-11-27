"""Unit tests for :class:`app.modules.logging.buffer.LogBuffer` (fake DB, real files)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.logging.buffer import LogBuffer, event_to_row
from app.modules.logging.events import ActivityEvent
from app.modules.logging.files import DailyFileWriter
from app.modules.logging.metrics import LogsDroppedCounter

pytestmark = pytest.mark.req("R7")

BOT = 7_000_000_001


def ev(user: int = 1, action: str = "CLICK") -> ActivityEvent:
    return ActivityEvent(
        telegram_bot_id=BOT,
        action_type=action,
        telegram_user_id=user,
        created_at=datetime(2026, 9, 14, 10, tzinfo=UTC),
    )


class FakeSession:
    def __init__(self, owner: FakeFactory) -> None:
        self.owner = owner

    async def execute(self, _stmt: Any, rows: Sequence[dict[str, Any]]) -> None:
        self.owner.calls += 1
        if self.owner.fail_times > 0:
            self.owner.fail_times -= 1
            raise RuntimeError("db down")
        if self.owner.delay:
            await asyncio.sleep(self.owner.delay)
        self.owner.rows.extend(rows)


class FakeFactory:
    def __init__(self, fail_times: int = 0, delay: float = 0.0) -> None:
        self.fail_times = fail_times
        self.delay = delay
        self.calls = 0
        self.rows: list[dict[str, Any]] = []

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[FakeSession]:
        yield FakeSession(self)


def make_buffer(
    tmp_path: Path, factory: FakeFactory, counter: LogsDroppedCounter, **kwargs: Any
) -> LogBuffer:
    params: dict[str, Any] = {
        "flush_interval_seconds": 0.05,
        "db_retry_backoff_seconds": 0.0,
        "prune_interval_seconds": None,
        "counter": counter,
    }
    params.update(kwargs)
    return LogBuffer(
        cast(async_sessionmaker[AsyncSession], factory), DailyFileWriter(tmp_path), **params
    )


@pytest.fixture
def counter() -> LogsDroppedCounter:
    return LogsDroppedCounter()


@pytest.mark.req("S5")
def test_counter_by_reason() -> None:
    counter = LogsDroppedCounter()
    counter.inc(reason="a")
    counter.inc(3, reason="b")
    counter.inc(0, reason="c")
    assert counter.value == 4
    assert counter.by_reason == {"a": 1, "b": 3}


def test_event_to_row_maps_metadata() -> None:
    row = event_to_row(
        ActivityEvent(telegram_bot_id=BOT, action_type="X", metadata={"k": 1})  # noqa: S106
    )
    assert row["metadata_"] == {"k": 1}
    assert "metadata" not in row
    assert isinstance(row["id"], UUID)


@pytest.mark.req("S5")
async def test_log_before_start_and_after_stop_is_dropped(
    tmp_path: Path, counter: LogsDroppedCounter, capsys: pytest.CaptureFixture[str]
) -> None:
    buffer = make_buffer(tmp_path, FakeFactory(), counter)
    buffer.log(ev())
    await buffer.start()
    await buffer.stop(1)
    buffer.log(ev(action="AFTER"))
    assert counter.by_reason == {"not_running": 2}
    err = capsys.readouterr().err
    assert err.count("LOG_DROPPED reason=not_running | ") == 2
    assert "action=AFTER" in err


@pytest.mark.req("S5")
async def test_queue_full_goes_to_stderr(
    tmp_path: Path, counter: LogsDroppedCounter, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = FakeFactory()
    buffer = make_buffer(tmp_path, factory, counter, queue_size=2)
    await buffer.start()
    for i in range(5):  # no await in between: the consumer cannot run
        buffer.log(ev(user=i))
    assert counter.by_reason == {"queue_full": 3}
    assert capsys.readouterr().err.count("LOG_DROPPED reason=queue_full") == 3
    await buffer.stop(5)
    assert len(factory.rows) == 2


@pytest.mark.req("S5")
async def test_db_failure_retries_then_drops_but_files_written(
    tmp_path: Path, counter: LogsDroppedCounter, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = FakeFactory(fail_times=3)
    buffer = make_buffer(tmp_path, factory, counter, db_retry_attempts=3)
    await buffer.start()
    buffer.log(ev(user=7))
    await asyncio.sleep(0.2)
    assert factory.calls == 3
    assert counter.by_reason == {"db_error": 1}
    assert "LOG_DROPPED reason=db_error" in capsys.readouterr().err
    assert (tmp_path / "users/2026-09-14/user_7.log").read_text().count("\n") == 1
    # The consumer is alive for the next batch.
    buffer.log(ev(user=8))
    await buffer.stop(5)
    assert [row["telegram_user_id"] for row in factory.rows] == [8]


@pytest.mark.req("S5")
async def test_file_failure_does_not_block_db(tmp_path: Path, counter: LogsDroppedCounter) -> None:
    (tmp_path / "users").write_text("blocker")
    factory = FakeFactory()
    buffer = make_buffer(tmp_path, factory, counter)
    await buffer.start()
    buffer.log(ev())
    await buffer.stop(5)
    assert len(factory.rows) == 1
    assert counter.by_reason == {"file_error": 1}


async def test_stop_drains_queue(tmp_path: Path, counter: LogsDroppedCounter) -> None:
    factory = FakeFactory()
    buffer = make_buffer(tmp_path, factory, counter, batch_size=10, flush_interval_seconds=30)
    await buffer.start()
    for i in range(35):
        buffer.log(ev(user=i))
    await buffer.stop(5)
    assert len(factory.rows) == 35
    assert counter.value == 0
    assert not buffer.running


@pytest.mark.parametrize("count", [0, 1], ids=["empty", "partial"])
async def test_stop_wakes_waiting_collector(
    tmp_path: Path, counter: LogsDroppedCounter, count: int
) -> None:
    factory = FakeFactory()
    buffer = make_buffer(tmp_path, factory, counter, batch_size=10, flush_interval_seconds=300)
    await buffer.start()
    for _ in range(count):
        buffer.log(ev())
    # Let the consumer enter the wait with an empty queue or a partial batch.
    await asyncio.sleep(0)
    assert factory.rows == []
    await asyncio.wait_for(buffer.stop(5), timeout=1)
    assert len(factory.rows) == count
    assert counter.value == 0
    assert not buffer.running


@pytest.mark.req("S5")
async def test_stop_timeout_dumps_rest(
    tmp_path: Path, counter: LogsDroppedCounter, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = FakeFactory(delay=5)
    buffer = make_buffer(tmp_path, factory, counter, batch_size=2)
    await buffer.start()
    for i in range(5):
        buffer.log(ev(user=i))
    await asyncio.sleep(0.05)
    await buffer.stop(0.2)
    assert counter.by_reason == {"shutdown_timeout": 5}
    assert capsys.readouterr().err.count("reason=shutdown_timeout") == 5


async def test_stop_budget_includes_blocked_file_writer(
    tmp_path: Path, counter: LogsDroppedCounter, monkeypatch: pytest.MonkeyPatch
) -> None:
    buffer = make_buffer(tmp_path, FakeFactory(), counter, batch_size=1)
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    write_line, close_all = buffer._writer._write_line, buffer._writer.close_all

    def blocked_write(path: Path, line: str) -> None:
        started.set()
        if not release.wait(5):
            raise OSError("test writer did not unblock")
        write_line(path, line)

    def track_close() -> None:
        close_all()
        closed.set()

    monkeypatch.setattr(buffer._writer, "_write_line", blocked_write)
    monkeypatch.setattr(buffer._writer, "close_all", track_close)
    await buffer.start()
    buffer.log(ev())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.wait_for(buffer.stop(0.05), timeout=1)
        assert not closed.is_set()
        assert counter.by_reason == {"shutdown_timeout": 1}
    finally:
        release.set()
    assert await asyncio.to_thread(closed.wait, 1)


async def test_log_from_other_thread(tmp_path: Path, counter: LogsDroppedCounter) -> None:
    factory = FakeFactory()
    buffer = make_buffer(tmp_path, factory, counter)
    await buffer.start()

    def produce() -> None:
        for i in range(20):
            buffer.log(ev(user=i))

    thread = threading.Thread(target=produce)
    thread.start()
    await asyncio.to_thread(thread.join)
    await asyncio.sleep(0)
    await buffer.stop(5)
    assert len(factory.rows) == 20
    assert counter.value == 0


async def test_prune_runs_outside_consumer(
    tmp_path: Path, counter: LogsDroppedCounter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RX2: a slow prune must not stall batch writing; ``stop`` cancels the prune task."""
    from app.modules.logging import buffer as buffer_module

    started, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def slow_prune(root: Path, today: object, retention: int) -> None:
        calls.append(retention)
        started.set()
        release.wait(5)

    monkeypatch.setattr(buffer_module, "prune_and_compress", slow_prune)
    factory = FakeFactory()
    buffer = make_buffer(tmp_path, factory, counter, prune_interval_seconds=3600)
    await buffer.start()
    assert await asyncio.to_thread(started.wait, 5)
    buffer.log(ev())
    for _ in range(50):
        if factory.rows:
            break
        await asyncio.sleep(0.02)
    assert len(factory.rows) == 1  # written while prune is still running
    release.set()
    await buffer.stop(5)
    assert calls == [90]  # fake factory -> retention fallback, once per interval
    assert counter.value == 0


@pytest.mark.req("S5")
async def test_log_never_raises_on_broken_event(
    tmp_path: Path, counter: LogsDroppedCounter
) -> None:
    buffer = make_buffer(tmp_path, FakeFactory(), counter)
    buffer.log(cast(ActivityEvent, object()))
    assert counter.by_reason == {"not_running": 1}
