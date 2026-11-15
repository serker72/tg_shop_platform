"""Text click-logs on the persistent volume (TZ 47.3, 47.4, 47.9).

Layout under ``Settings.logs.dir`` (``/app/logs`` in the container, the TZ layout verbatim)::

    users/<YYYY-MM-DD>/user_<telegram_user_id>.log   - events with a Telegram user
    bots/bot_<telegram_bot_id>/<YYYY-MM-DD>.log      - every event (``telegram_bot_id`` NOT NULL)
    system/<YYYY-MM-DD>.log                          - ``source == "system"`` or no user

One event is written to several files on purpose: the TZ requires both a sequential log per
user and per-bot/system logs; duplication of a short line is cheaper than a reader that has
to merge files.

Line format (TZ 47.3 example, ``|``-separated ``key=value``; empty fields omitted)::

    2026-09-14 18:42:31 | bot=<telegram_bot_id> | user=123456 | username=@example
    | action=OPEN_PRODUCT | category=<uuid> | subcategory=<uuid> | product=<uuid> | result=success

Time is **UTC** (``YYYY-MM-DD HH:MM:SS``) like the DB (TZ 47.2); the date of the file is the
UTC date of ``created_at``. ``metadata`` is appended last as compact JSON (``meta={...}``).
Values are sanitised: CR/LF -> ``\\n``, ``|`` -> ``/`` (one record = one line).

Size limit (TZ 47.9): when ``<name>.log`` would exceed ``max_file_bytes`` the next lines go to
``<name>.1.log``, then ``<name>.2.log``... Files are never renamed, so two processes
(``master_bot`` / ``bot_worker``) appending to the same directory never race on a rename;
each append is one ``os.write`` of a whole line on an ``O_APPEND`` descriptor (atomic
positioning on a local filesystem). The limit is soft: two processes may both append to a
file that is just below the limit.
"""

from __future__ import annotations

import contextlib
import fcntl
import gzip
import json
import os
import re
import shutil
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, timedelta
from pathlib import Path
from typing import Final

import structlog

from app.modules.logging.events import ActivityEvent

__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_OPEN_FILES",
    "DailyFileWriter",
    "PruneReport",
    "event_paths",
    "format_line",
    "prune_and_compress",
]

DEFAULT_MAX_OPEN_FILES: Final = 256
DEFAULT_MAX_FILE_BYTES: Final = 50 * 1024 * 1024
#: Inter-process lock of :func:`prune_and_compress` (``flock``), kept under ``root``.
PRUNE_LOCK_NAME: Final = ".prune.lock"
_FILE_MODE: Final = 0o640
_DIR_MODE: Final = 0o750
_DATE_RE: Final = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TMP_SUFFIX: Final = ".tmp"

_log = structlog.get_logger(__name__)

_REFERENCE_FIELDS: Final = (
    ("category", "category_id"),
    ("subcategory", "subcategory_id"),
    ("product", "product_id"),
    ("order", "order_id"),
    ("case", "case_id"),
    ("preorder", "preorder_id"),
)


def _sanitize(value: str) -> str:
    return value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n").replace("|", "/")


def format_line(event: ActivityEvent) -> str:
    """Render ``event`` as one text line without the trailing newline (UTC timestamp)."""
    parts = [event.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")]

    def add(key: str, value: object) -> None:
        if value is None or value == "":
            return
        parts.append(f"{key}={_sanitize(str(value))}")

    username = event.username
    if username and not username.startswith("@"):
        username = f"@{username}"
    add("bot", event.telegram_bot_id)
    add("user", event.telegram_user_id)
    add("username", username)
    add("user_type", event.user_type)
    add("role", event.role)
    add("source", event.source)
    add("action", event.action_type)
    if event.entity_type is not None or event.entity_id is not None:
        add("entity", f"{event.entity_type or ''}:{event.entity_id or ''}")
    for key, attr in _REFERENCE_FIELDS:
        add(key, getattr(event, attr))
    add("button", event.button)
    add("callback", event.callback_data)
    add("screen_from", event.screen_from)
    add("screen", event.screen_current)
    add("screen_to", event.screen_to)
    add("tenant", event.tenant_id)
    add("result", event.result)
    if event.metadata:
        add(
            "meta",
            json.dumps(
                dict(event.metadata), ensure_ascii=False, separators=(",", ":"), default=str
            ),
        )
    return " | ".join(parts)


def is_system_event(event: ActivityEvent) -> bool:
    """System event: ``source == "system"`` or no Telegram user."""
    return event.source == "system" or event.telegram_user_id is None


def event_paths(event: ActivityEvent) -> list[Path]:
    """Relative target files of ``event`` (see the module docstring)."""
    day = event.created_at.astimezone(UTC).date().isoformat()
    paths: list[Path] = []
    if event.telegram_user_id is not None:
        paths.append(Path("users", day, f"user_{event.telegram_user_id}.log"))
    paths.append(Path("bots", f"bot_{event.telegram_bot_id}", f"{day}.log"))
    if is_system_event(event):
        paths.append(Path("system", f"{day}.log"))
    return paths


def _sized_path(base: Path, index: int) -> Path:
    if index == 0:
        return base
    return base.with_name(f"{base.stem}.{index}{base.suffix}")


class DailyFileWriter:
    """Append-only writer with an LRU cache of open descriptors.

    Synchronous; public methods are serialised by an internal ``RLock``. ``LogBuffer`` calls
    them via ``asyncio.to_thread``; a cancelled ``to_thread`` keeps running in its thread, so
    ``close_all`` from ``stop()`` waits for an in-flight ``write_events`` instead of closing
    descriptors under it (no EBADF / write into a reused descriptor number).

    ``_index`` keeps only overflow indices ``> 0`` (oversized files), so it does not grow
    with the number of users/days; ``close_all`` clears it (the index is re-probed).
    """

    def __init__(
        self,
        root: Path,
        *,
        max_open_files: int = DEFAULT_MAX_OPEN_FILES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        if max_open_files <= 0 or max_file_bytes <= 0:
            raise ValueError("max_open_files and max_file_bytes must be > 0")
        self.root = root
        self._max_open = max_open_files
        self._max_bytes = max_file_bytes
        self._fds: OrderedDict[Path, int] = OrderedDict()
        self._index: dict[Path, int] = {}
        self._lock = threading.RLock()

    @property
    def open_files(self) -> int:
        """Number of currently cached descriptors."""
        return len(self._fds)

    @property
    def tracked_rollovers(self) -> int:
        """Number of base files remembered with an overflow index (``name.N.log``)."""
        return len(self._index)

    def _close(self, path: Path) -> None:
        fd = self._fds.pop(path, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _get_fd(self, path: Path) -> int:
        fd = self._fds.get(path)
        if fd is not None:
            # The file may have been compressed/removed by ``prune_and_compress``.
            if os.fstat(fd).st_nlink > 0:
                self._fds.move_to_end(path)
                return fd
            self._close(path)
        path.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, _FILE_MODE)
        self._fds[path] = fd
        while len(self._fds) > self._max_open:
            oldest = next(iter(self._fds))
            self._close(oldest)
        return fd

    def write_line(self, relative: Path, line: str) -> None:
        """Append ``line`` + ``\\n`` to ``root / relative`` (size-limited, one ``os.write``)."""
        with self._lock:
            self._write_line(relative, line)

    def _write_line(self, relative: Path, line: str) -> None:
        data = (line + "\n").encode("utf-8")
        base = self.root / relative
        index = self._index.get(base, 0)
        while True:
            path = _sized_path(base, index)
            fd = self._get_fd(path)
            size = os.fstat(fd).st_size
            if size > 0 and size + len(data) > self._max_bytes:
                self._close(path)
                index += 1
                continue
            break
        if index > 0:
            self._index[base] = index
        written = os.write(fd, data)
        if written != len(data):
            raise OSError(f"short write to {path}: {written} of {len(data)} bytes")

    def write_events(self, events: Iterable[ActivityEvent]) -> list[ActivityEvent]:
        """Write every event to all its files; return events with at least one failed file."""
        with self._lock:
            return self._write_events(events)

    def _write_events(self, events: Iterable[ActivityEvent]) -> list[ActivityEvent]:
        failed: list[ActivityEvent] = []
        for event in events:
            ok = True
            try:
                line = format_line(event)
                paths = event_paths(event)
            except Exception as error:
                _log.warning("log_file_format_failed", error=repr(error))
                failed.append(event)
                continue
            for relative in paths:
                try:
                    self._write_line(relative, line)
                except OSError as error:
                    ok = False
                    _log.warning("log_file_write_failed", path=str(relative), error=repr(error))
            if not ok:
                failed.append(event)
        return failed

    def close_all(self) -> None:
        """Close every cached descriptor (waits for an in-flight write)."""
        with self._lock:
            for path in list(self._fds):
                self._close(path)
            self._index.clear()


@dataclass(slots=True)
class PruneReport:
    """Result of :func:`prune_and_compress`."""

    compressed: list[Path]
    deleted: list[Path]
    removed_dirs: list[Path]
    #: ``True`` - another process holds the prune lock, nothing was done.
    locked: bool = False


@contextlib.contextmanager
def _prune_lock(root: Path) -> Iterator[bool]:
    """Non-blocking exclusive ``flock`` on ``root/.prune.lock``; yields ``False`` if busy."""
    fd = os.open(root / PRUNE_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, _FILE_MODE)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _date_of(relative: Path) -> date | None:
    match = _DATE_RE.search(relative.as_posix())
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _gzip_file(path: Path) -> Path:
    """Compress ``path`` into ``<path>.gz`` atomically and remove ``path``.

    An existing ``.gz`` (late lines written after a previous compression) is kept and a new
    gzip member is appended (concatenated members are valid gzip), so nothing is lost.
    """
    target = path.with_name(path.name + ".gz")
    tmp = target.with_name(f"{target.name}.{os.getpid()}{_TMP_SUFFIX}")
    try:
        with tmp.open("wb") as out:
            if target.exists():
                with target.open("rb") as existing:
                    shutil.copyfileobj(existing, out)
            with path.open("rb") as src, gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
                shutil.copyfileobj(src, gz)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    path.unlink(missing_ok=True)
    return target


def prune_and_compress(
    root: Path,
    today: date,
    retention_days: int,
    *,
    min_age_seconds: float = 3600.0,
    now: float | None = None,
) -> PruneReport:
    """Compress past days and delete expired files under ``root``; idempotent.

    * the date of a file is the first ``YYYY-MM-DD`` in its path relative to ``root``;
    * ``date >= today`` - untouched;
    * ``date < today - retention_days`` - deleted (``.log`` and ``.gz``);
    * other past ``.log`` files are gzipped once they were not modified for
      ``min_age_seconds`` (late events flushed right after midnight still land in the
      plain file);
    * empty date directories are removed; stale ``*.tmp`` of crashed runs are deleted.

    ``master_bot`` and ``bot_worker`` share the directory: a run holds a non-blocking
    ``flock`` on ``root/.prune.lock``; if another process holds it, the run is skipped
    (``report.locked``). The lock file has no date and is never pruned.

    Errors on a single file are logged and skipped.
    """
    report = PruneReport(compressed=[], deleted=[], removed_dirs=[])
    if not root.is_dir():
        return report
    with _prune_lock(root) as acquired:
        if not acquired:
            report.locked = True
            _log.info("log_prune_skipped_locked", root=str(root))
            return report
        _prune_locked(root, today, retention_days, min_age_seconds, now, report)
    return report


def _prune_locked(
    root: Path,
    today: date,
    retention_days: int,
    min_age_seconds: float,
    now: float | None,
    report: PruneReport,
) -> None:
    current = time.time() if now is None else now
    cutoff = today - timedelta(days=retention_days)
    lock_path = root / PRUNE_LOCK_NAME
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p != lock_path):
        try:
            age = current - path.stat().st_mtime
            if path.name.endswith(_TMP_SUFFIX):
                if age >= min_age_seconds:
                    path.unlink(missing_ok=True)
                    report.deleted.append(path)
                continue
            day = _date_of(path.relative_to(root))
            if day is None or day >= today:
                continue
            if day < cutoff:
                path.unlink(missing_ok=True)
                report.deleted.append(path)
            elif path.suffix == ".log" and age >= min_age_seconds:
                report.compressed.append(_gzip_file(path))
        except OSError as error:
            _log.warning("log_prune_failed", path=str(path), error=repr(error))
    dirs: Sequence[Path] = sorted(
        (p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
    )
    for directory in dirs:
        if _DATE_RE.fullmatch(directory.name) is None:
            continue
        try:
            if not any(directory.iterdir()):
                directory.rmdir()
                report.removed_dirs.append(directory)
        except OSError as error:
            _log.warning("log_prune_failed", path=str(directory), error=repr(error))
