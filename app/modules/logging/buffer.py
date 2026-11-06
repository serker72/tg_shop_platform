"""Asynchronous click-log buffer (TZ 47.8): DB + text files, never breaks the caller.

``LogBuffer.log`` only enqueues (``put_nowait``). A background consumer collects batches
(``batch_size`` or ``flush_interval_seconds``) and writes them independently to

* the DB - one ``INSERT`` per batch in its own transaction, ``db_retry_attempts`` tries
  with exponential backoff, then the batch goes to ``stderr`` (``reason="db_error"``).
  A data error (``IntegrityError`` / ``DataError``: NOT NULL, CHECK, length) is not
  retried: the batch is split in halves recursively, so only the offending rows are lost
  (``reason="db_rejected"``);
* the text files (:mod:`app.modules.logging.files`) in a worker thread; failed events go to
  ``stderr`` (``reason="file_error"``).

Every lost record is printed to ``stderr`` as ``LOG_DROPPED reason=<r> | <file line>`` and
counted in :data:`~app.modules.logging.metrics.logs_dropped_total`. Errors of the buffer
itself are logged through structlog (stdout), never through the buffer.

Threads: ``log`` may be called from any thread. Calls from a thread other than the one
running the event loop of ``start()`` are handed over with ``loop.call_soon_threadsafe``
(``asyncio.Queue`` is not thread-safe).

File pruning/compression runs in a separate background task (at start, then every
``prune_interval_seconds``), so a long prune never stalls the consumer.

Delivery is at-most-once to each sink: if ``stop`` times out while a batch is being
written, that batch is printed to ``stderr`` even if part of it may already be stored.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from collections.abc import Sequence
from dataclasses import fields
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import insert, select
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.modules.logging.events import ActivityEvent
from app.modules.logging.files import DailyFileWriter, format_line, prune_and_compress
from app.modules.logging.metrics import LogsDroppedCounter, logs_dropped_total
from app.modules.logging.models import UserActivity
from app.modules.logging.ports import ActivityLogger
from app.modules.system.models import (
    DEFAULT_FILE_LOGS_RETENTION_DAYS,
    SINGLETON_ID,
    SystemSettings,
)

__all__ = [
    "LogBuffer",
    "LogsDroppedCounter",
    "event_to_row",
    "read_file_logs_retention_days",
]

_log = structlog.get_logger(__name__)


def event_to_row(event: ActivityEvent) -> dict[str, Any]:
    """Map an event to ``UserActivity`` attribute values (new ``id``)."""
    row: dict[str, Any] = {f.name: getattr(event, f.name) for f in fields(event)}
    metadata = row.pop("metadata")
    row["metadata_"] = dict(metadata) if metadata is not None else None
    row["id"] = uuid4()
    return row


async def read_file_logs_retention_days(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Read ``system_settings.file_logs_retention_days``; default 90 on any error."""
    try:
        async with session_factory() as session:
            value = await session.scalar(
                select(SystemSettings.file_logs_retention_days).where(
                    SystemSettings.id == SINGLETON_ID
                )
            )
    except Exception as error:
        _log.warning("file_logs_retention_read_failed", error=repr(error))
        return DEFAULT_FILE_LOGS_RETENTION_DAYS
    return int(value) if value is not None else DEFAULT_FILE_LOGS_RETENTION_DAYS


class LogBuffer(ActivityLogger):
    """Bounded in-memory queue + background writer (see the module docstring)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        writer: DailyFileWriter,
        *,
        queue_size: int = 10_000,
        batch_size: int = 500,
        flush_interval_seconds: float = 1.0,
        db_retry_attempts: int = 3,
        db_retry_backoff_seconds: float = 0.5,
        prune_interval_seconds: float | None = 3600.0,
        counter: LogsDroppedCounter = logs_dropped_total,
    ) -> None:
        if queue_size <= 0 or batch_size <= 0 or db_retry_attempts <= 0:
            raise ValueError("queue_size, batch_size and db_retry_attempts must be > 0")
        if flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be > 0")
        self._session_factory = session_factory
        self._writer = writer
        self._queue: asyncio.Queue[ActivityEvent] = asyncio.Queue(maxsize=queue_size)
        self._batch_size = batch_size
        self._flush_interval = flush_interval_seconds
        self._db_attempts = db_retry_attempts
        self._db_backoff = db_retry_backoff_seconds
        self._prune_interval = prune_interval_seconds
        self._counter = counter
        self._accepting = False
        self._closing = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread_id: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._inflight: list[ActivityEvent] = []
        self._prune_task: asyncio.Task[None] | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        counter: LogsDroppedCounter = logs_dropped_total,
    ) -> LogBuffer:
        """Build the buffer and its file writer from ``Settings.logs``."""
        logs = settings.logs
        writer = DailyFileWriter(
            logs.dir, max_open_files=logs.file_max_open, max_file_bytes=logs.file_max_bytes
        )
        return cls(
            session_factory,
            writer,
            queue_size=logs.buffer_queue_size,
            batch_size=logs.buffer_batch_size,
            flush_interval_seconds=logs.buffer_flush_interval_seconds,
            db_retry_attempts=logs.buffer_db_retry_attempts,
            counter=counter,
        )

    @property
    def running(self) -> bool:
        """``True`` between ``start()`` and ``stop()``."""
        return self._accepting

    # -- producer side -------------------------------------------------------------------

    def log(self, event: ActivityEvent) -> None:
        """Enqueue ``event``; never blocks, never raises."""
        try:
            loop = self._loop
            if loop is not None and threading.get_ident() != self._thread_id:
                loop.call_soon_threadsafe(self._enqueue, event)
                return
            self._enqueue(event)
        except Exception:
            self._drop([event], "not_running")

    def _enqueue(self, event: ActivityEvent) -> None:
        if not self._accepting:
            self._drop([event], "not_running")
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._drop([event], "queue_full")

    def _drop(self, events: Sequence[ActivityEvent], reason: str) -> None:
        if not events:
            return
        with contextlib.suppress(Exception):
            self._counter.inc(len(events), reason=reason)
        for event in events:
            try:
                line = format_line(event)
            except Exception:
                line = repr(event).replace("\n", "\\n")
            with contextlib.suppress(Exception):
                sys.stderr.write(f"LOG_DROPPED reason={reason} | {line}\n")
        with contextlib.suppress(Exception):
            sys.stderr.flush()

    # -- lifecycle -----------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background consumer (idempotent)."""
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._thread_id = threading.get_ident()
        self._closing = False
        self._accepting = True
        self._task = asyncio.create_task(self._run(), name="log-buffer-consumer")
        if self._prune_interval is not None:
            self._prune_task = asyncio.create_task(
                self._prune_loop(self._prune_interval), name="log-files-prune"
            )

    async def stop(self, timeout: float = 10.0) -> None:  # noqa: ASYNC109 - drain deadline
        """Stop accepting, drain the queue; leftovers after ``timeout`` go to ``stderr``."""
        self._accepting = False
        task = self._task
        if task is None:
            return
        self._closing = True
        prune_task, self._prune_task = self._prune_task, None
        if prune_task is not None:
            prune_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await prune_task
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            leftover = list(self._inflight)
            while True:
                try:
                    leftover.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            self._inflight = []
            self._drop(leftover, "shutdown_timeout")
        except Exception as error:
            _log.error("log_buffer_stop_failed", error=repr(error))
        finally:
            self._task = None
            self._loop = None
            self._thread_id = None
            try:
                await asyncio.to_thread(self._writer.close_all)
            except Exception as error:
                _log.warning("log_files_close_failed", error=repr(error))

    # -- consumer side -------------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                batch = await self._collect()
                if batch:
                    self._inflight = batch
                    await self._flush(batch)
                    self._inflight = []
            except Exception as error:  # the consumer must survive any batch
                _log.error("log_buffer_batch_failed", error=repr(error))
                self._drop(self._inflight, "internal_error")
                self._inflight = []
            if self._closing and self._queue.empty():
                return

    async def _collect(self) -> list[ActivityEvent]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._flush_interval
        batch: list[ActivityEvent] = []
        while len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
                continue
            except asyncio.QueueEmpty:
                pass
            remaining = deadline - loop.time()
            if self._closing or remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(self._queue.get(), remaining))
            except TimeoutError:
                break
        return batch

    async def _flush(self, batch: list[ActivityEvent]) -> None:
        await asyncio.gather(self._write_db(batch), self._write_files(batch))

    async def _write_db(self, batch: list[ActivityEvent]) -> None:
        await self._write_db_chunk([(event, event_to_row(event)) for event in batch])

    async def _write_db_chunk(self, items: list[tuple[ActivityEvent, dict[str, Any]]]) -> None:
        batch = [event for event, _ in items]
        rows = [row for _, row in items]
        for attempt in range(1, self._db_attempts + 1):
            try:
                async with self._session_factory.begin() as session:
                    await session.execute(insert(UserActivity), rows)
            except (IntegrityError, DataError) as error:
                if len(items) == 1:
                    _log.warning("log_buffer_db_row_rejected", error=repr(error))
                    self._drop(batch, "db_rejected")
                    return
                middle = len(items) // 2
                await self._write_db_chunk(items[:middle])
                await self._write_db_chunk(items[middle:])
                return
            except Exception as error:
                _log.warning(
                    "log_buffer_db_write_failed",
                    attempt=attempt,
                    attempts=self._db_attempts,
                    batch=len(batch),
                    error=repr(error),
                )
                if attempt < self._db_attempts:
                    await asyncio.sleep(self._db_backoff * 2 ** (attempt - 1))
            else:
                return
        self._drop(batch, "db_error")

    async def _write_files(self, batch: list[ActivityEvent]) -> None:
        try:
            failed = await asyncio.to_thread(self._writer.write_events, batch)
        except Exception as error:
            _log.warning("log_buffer_file_write_failed", error=repr(error))
            failed = batch
        self._drop(failed, "file_error")

    async def _prune_loop(self, interval: float) -> None:
        while True:
            try:
                await self._prune_once()
            except Exception as error:  # the loop must survive any run
                _log.warning("log_files_prune_failed", error=repr(error))
            await asyncio.sleep(interval)

    async def _prune_once(self) -> None:
        retention = await read_file_logs_retention_days(self._session_factory)
        today = datetime.now(UTC).date()
        try:
            await asyncio.to_thread(prune_and_compress, self._writer.root, today, retention)
        except Exception as error:
            _log.warning("log_files_prune_failed", error=repr(error))
