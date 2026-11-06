"""Unit tests for :mod:`app.modules.logging.files`."""

from __future__ import annotations

import gzip
import multiprocessing
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from app.modules.logging.events import ActivityEvent
from app.modules.logging.files import DailyFileWriter, event_paths, format_line, prune_and_compress

BOT = UUID("00000000-0000-0000-0000-000000000017")
PRODUCT = UUID("00000000-0000-0000-0000-000000000384")
CATEGORY = UUID("00000000-0000-0000-0000-000000000005")
TS = datetime(2026, 9, 14, 18, 42, 31, tzinfo=UTC)


def make_event(**overrides: object) -> ActivityEvent:
    data: dict[str, object] = {
        "bot_id": BOT,
        "action_type": "OPEN_PRODUCT",
        "telegram_user_id": 123456,
        "username": "example",
        "category_id": CATEGORY,
        "product_id": PRODUCT,
        "result": "success",
        "created_at": TS,
    }
    data.update(overrides)
    return ActivityEvent(**data)  # type: ignore[arg-type]


def test_format_line_matches_tz_example() -> None:
    assert format_line(make_event()) == (
        f"2026-09-14 18:42:31 | bot={BOT} | user=123456 | username=@example "
        f"| action=OPEN_PRODUCT | category={CATEGORY} | product={PRODUCT} | result=success"
    )


def test_format_line_utc_sanitize_and_metadata() -> None:
    from datetime import timezone

    local = TS.astimezone(timezone(timedelta(hours=3)))
    line = format_line(
        make_event(
            created_at=local,
            button="Buy\nnow|x\r",
            metadata={"a": 1, "b": "т|"},
            category_id=None,
        )
    )
    assert line.startswith("2026-09-14 18:42:31 | ")
    assert "\n" not in line and "\r" not in line
    assert "button=Buy\\nnow/x\\n" in line
    assert line.endswith('meta={"a":1,"b":"т/"}')
    assert "category=" not in line


def test_naive_created_at_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        make_event(created_at=datetime(2026, 1, 1))  # noqa: DTZ001


def test_event_paths_layout() -> None:
    assert event_paths(make_event()) == [
        Path("users/2026-09-14/user_123456.log"),
        Path(f"bots/bot_{BOT}/2026-09-14.log"),
    ]
    assert event_paths(make_event(telegram_user_id=None)) == [
        Path(f"bots/bot_{BOT}/2026-09-14.log"),
        Path("system/2026-09-14.log"),
    ]
    assert Path("system/2026-09-14.log") in event_paths(make_event(source="system"))


def test_writer_day_change_and_close(tmp_path: Path) -> None:
    writer = DailyFileWriter(tmp_path)
    writer.write_events([make_event(), make_event(created_at=TS + timedelta(days=1))])
    writer.close_all()
    assert (tmp_path / "users/2026-09-14/user_123456.log").read_text().count("\n") == 1
    assert (tmp_path / "users/2026-09-15/user_123456.log").exists()
    assert (tmp_path / f"bots/bot_{BOT}/2026-09-15.log").exists()
    assert writer.open_files == 0
    mode = (tmp_path / "users/2026-09-14/user_123456.log").stat().st_mode & 0o777
    assert mode & ~0o640 == 0


def test_writer_size_limit_goes_to_numbered_files(tmp_path: Path) -> None:
    line = "x" * 40
    writer = DailyFileWriter(tmp_path, max_file_bytes=100)
    for _ in range(5):
        writer.write_line(Path("system/2026-09-14.log"), line)
    writer.close_all()
    names = sorted(p.name for p in (tmp_path / "system").iterdir())
    assert names == ["2026-09-14.1.log", "2026-09-14.2.log", "2026-09-14.log"]
    assert all(p.stat().st_size <= 100 for p in (tmp_path / "system").iterdir())
    # A fresh writer (other process) continues after the full files.
    other = DailyFileWriter(tmp_path, max_file_bytes=100)
    other.write_line(Path("system/2026-09-14.log"), line)
    other.close_all()
    assert (tmp_path / "system/2026-09-14.2.log").read_text().count("\n") == 2


def test_writer_lru_closes_descriptors(tmp_path: Path) -> None:
    writer = DailyFileWriter(tmp_path, max_open_files=2)
    for i in range(5):
        writer.write_line(Path(f"system/f{i}.log"), "line")
    assert writer.open_files == 2
    writer.close_all()
    assert len(list((tmp_path / "system").iterdir())) == 5


def test_write_events_reports_failed(tmp_path: Path) -> None:
    (tmp_path / "users").write_text("not a dir")
    writer = DailyFileWriter(tmp_path)
    failed = writer.write_events([make_event()])
    writer.close_all()
    assert len(failed) == 1
    assert (tmp_path / f"bots/bot_{BOT}/2026-09-14.log").exists()


def _write_many(root: str, tag: str, count: int) -> None:
    writer = DailyFileWriter(Path(root))
    for i in range(count):
        writer.write_line(Path("system/shared.log"), f"{tag}-{i:05d}-" + tag * 2000)
    writer.close_all()


def test_two_processes_write_whole_lines(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_write_many, args=(str(tmp_path), t, 300)) for t in "ab"]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    lines = (tmp_path / "system/shared.log").read_text().splitlines()
    assert len(lines) == 600
    for line in lines:
        tag = line[0]
        assert line == f"{tag}-{line[2:7]}-" + tag * 2000


def _touch_old(path: Path, now: float) -> None:
    os.utime(path, (now - 7200, now - 7200))


def test_prune_and_compress(tmp_path: Path) -> None:
    now = datetime(2026, 9, 14, 12, tzinfo=UTC).timestamp()
    today = date(2026, 9, 14)
    files = {
        "today": tmp_path / "users/2026-09-14/user_1.log",
        "yesterday": tmp_path / "users/2026-09-13/user_1.log",
        "bot_yesterday": tmp_path / "bots/bot_x/2026-09-13.log",
        "expired": tmp_path / "users/2026-06-01/user_1.log",
        "expired_gz": tmp_path / "system/2026-06-01.log.gz",
    }
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"line\n")
        _touch_old(path, now)

    report = prune_and_compress(tmp_path, today, 30, now=now)

    assert files["today"].read_bytes() == b"line\n"
    assert not files["yesterday"].exists()
    assert gzip.decompress(files["yesterday"].with_name("user_1.log.gz").read_bytes()) == (
        b"line\n"
    )
    assert (tmp_path / "bots/bot_x/2026-09-13.log.gz").exists()
    assert not files["expired"].exists() and not files["expired_gz"].exists()
    assert not (tmp_path / "users/2026-06-01").exists()
    assert len(report.compressed) == 2

    again = prune_and_compress(tmp_path, today, 30, now=now)
    assert again.compressed == [] and again.deleted == []

    # Late lines after compression are appended as a new gzip member.
    files["yesterday"].write_bytes(b"late\n")
    _touch_old(files["yesterday"], now)
    prune_and_compress(tmp_path, today, 30, now=now)
    gz = files["yesterday"].with_name("user_1.log.gz").read_bytes()
    assert gzip.decompress(gz) == b"line\nlate\n"


def test_close_all_waits_for_inflight_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RX1: ``close_all`` from another thread must not close descriptors under a write."""
    import threading

    from app.modules.logging import files

    entered, release = threading.Event(), threading.Event()
    original = files.event_paths

    def slow_paths(event: ActivityEvent) -> list[Path]:
        paths = original(event)
        if event.telegram_user_id == 2:
            entered.set()
            release.wait(5)
        return paths

    monkeypatch.setattr(files, "event_paths", slow_paths)
    writer = DailyFileWriter(tmp_path)
    events = [make_event(telegram_user_id=1), make_event(telegram_user_id=2)]
    result: list[list[ActivityEvent]] = []
    write = threading.Thread(target=lambda: result.append(writer.write_events(events)))
    write.start()
    assert entered.wait(5)
    closed = threading.Event()

    def close_writer() -> None:
        writer.close_all()
        closed.set()

    close = threading.Thread(target=close_writer)
    close.start()
    assert not closed.wait(0.2)  # blocked by the in-flight write
    release.set()
    write.join(5)
    close.join(5)
    assert closed.is_set() and result == [[]] and writer.open_files == 0
    assert (tmp_path / f"bots/bot_{BOT}/2026-09-14.log").read_text().count("\n") == 2


def test_rollover_index_is_bounded(tmp_path: Path) -> None:
    """RX5: only overflow indices are remembered; ``close_all`` forgets them."""
    writer = DailyFileWriter(tmp_path, max_file_bytes=100)
    for i in range(50):
        writer.write_line(Path(f"users/2026-09-14/user_{i}.log"), "x")
    assert writer.tracked_rollovers == 0
    for _ in range(4):
        writer.write_line(Path("system/2026-09-14.log"), "y" * 40)
    assert writer.tracked_rollovers == 1
    writer.close_all()
    assert writer.tracked_rollovers == 0
    writer.write_line(Path("system/2026-09-14.log"), "y" * 40)
    writer.close_all()
    assert (tmp_path / "system/2026-09-14.1.log").read_text().count("\n") == 2


def test_prune_skipped_when_locked_by_other_process(tmp_path: Path) -> None:
    """RX4: a held ``.prune.lock`` (another process) makes the run a no-op."""
    import fcntl

    from app.modules.logging.files import PRUNE_LOCK_NAME

    now = datetime(2026, 9, 14, 12, tzinfo=UTC).timestamp()
    old = tmp_path / "system/2026-09-13.log"
    old.parent.mkdir(parents=True)
    old.write_text("x\n")
    _touch_old(old, now)
    fd = os.open(tmp_path / PRUNE_LOCK_NAME, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = prune_and_compress(tmp_path, date(2026, 9, 14), 30, now=now)
        assert report.locked and report.compressed == []
        assert old.exists()
    finally:
        os.close(fd)
    report = prune_and_compress(tmp_path, date(2026, 9, 14), 30, now=now)
    assert not report.locked and len(report.compressed) == 1
    assert (tmp_path / PRUNE_LOCK_NAME).exists()  # never pruned itself
    again = prune_and_compress(tmp_path, date(2026, 9, 14), 0, now=now + 10**9)
    assert (tmp_path / PRUNE_LOCK_NAME).exists() and again.deleted


def test_prune_skips_recently_modified(tmp_path: Path) -> None:
    path = tmp_path / "system/2026-09-13.log"
    path.parent.mkdir(parents=True)
    path.write_text("x\n")
    prune_and_compress(tmp_path, date(2026, 9, 14), 30)
    assert path.exists()
