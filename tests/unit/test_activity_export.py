from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.logging import export as export_module
from app.modules.logging.events import ActivityEvent
from app.modules.logging.export import GZIP_THRESHOLD, MAX_EXPORT_ROWS
from app.modules.logging.files import format_line
from app.modules.logging.models import UserActivity
from app.modules.logging.query import (
    ActivityFilter,
    ActivityQueryRepository,
    ActivityRow,
    ActivityScope,
)


def test_activity_format_uses_file_formatter() -> None:
    line = format_line(
        ActivityEvent(telegram_bot_id=1, action_type="OPEN", created_at=datetime.now(UTC))
    )
    assert "bot=1" in line and "action=OPEN" in line
    row = _row(1)
    assert export_module.format_export_line(row) == (
        format_line(export_module._event(row)) + f" | event_id={row.activity.id}"
    )


def test_export_limits_and_threshold_constants() -> None:
    assert MAX_EXPORT_ROWS == 100_000
    assert GZIP_THRESHOLD == 45 * 1024 * 1024


def _row(number: int) -> ActivityRow:
    return ActivityRow(
        activity=cast(
            UserActivity,
            SimpleNamespace(
                id=UUID(int=number),
                telegram_bot_id=number,
                action_type="OPEN",
                tenant_id=None,
                telegram_user_id=number,
                username="user",
                user_type=None,
                role=None,
                source=None,
                entity_type=None,
                entity_id=None,
                order_id=None,
                product_id=None,
                category_id=None,
                subcategory_id=None,
                case_id=None,
                preorder_id=None,
                button=None,
                callback_data=None,
                screen_from=None,
                screen_current=None,
                screen_to=None,
                metadata_=None,
                result="ok",
                created_at=datetime.now(UTC),
            ),
        )
    )


class _Repository:
    def __init__(self, rows: list[ActivityRow]) -> None:
        self.rows = rows
        self.calls = 0

    @asynccontextmanager
    async def stream_export(
        self, *_args: object, limit: int, **_kwargs: object
    ) -> AsyncIterator[Any]:
        self.calls += 1

        async def partitions() -> AsyncIterator[list[UserActivity]]:
            for row in self.rows[:limit]:
                yield [row.activity]

        yield SimpleNamespace(partitions=partitions)


@pytest.mark.asyncio
async def test_export_stops_at_max_rows_and_cleans_raw_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_module, "MAX_EXPORT_ROWS", 2)
    repository = _Repository([_row(1), _row(2), _row(3)])
    result = await export_module.export_txt(
        ActivityFilter(),
        repository=cast(ActivityQueryRepository, repository),
        logs_dir=tmp_path,
        scope=ActivityScope(frozenset(), all_tenants=True),
    )
    assert result.suffix == ".txt"
    assert repository.calls == 1
    assert [line.rsplit("event_id=", 1)[1] for line in result.read_text().splitlines()] == [
        str(UUID(int=1)),
        str(UUID(int=2)),
    ]
    assert result.read_text().count("action=OPEN") == 2
    assert not list((tmp_path / "system" / "tmp").glob("*.txt.gz"))


@pytest.mark.asyncio
async def test_export_gzips_above_threshold_and_removes_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_module, "GZIP_THRESHOLD", 1)
    result = await export_module.export_txt(
        ActivityFilter(),
        repository=cast(ActivityQueryRepository, _Repository([_row(1)])),
        logs_dir=tmp_path,
        scope=ActivityScope(frozenset(), all_tenants=True),
    )
    assert result.suffix == ".gz"
    assert result.exists()
    assert not result.with_suffix("").exists()


@pytest.mark.asyncio
async def test_export_gzip_failure_cleans_partial_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_module, "GZIP_THRESHOLD", 1)

    def failing_gzip(source: Path, target: Path) -> None:
        target.write_bytes(b"partial gzip")
        raise OSError("gzip failed")

    monkeypatch.setattr(export_module, "_gzip_file", failing_gzip)

    with pytest.raises(OSError, match="gzip failed"):
        await export_module.export_txt(
            ActivityFilter(),
            repository=cast(ActivityQueryRepository, _Repository([_row(1)])),
            logs_dir=tmp_path,
            scope=ActivityScope(frozenset(), all_tenants=True),
        )
    assert list((tmp_path / "system" / "tmp").iterdir()) == []


@pytest.mark.asyncio
async def test_export_failure_cleans_temporary_file(tmp_path: Path) -> None:
    class FailingRepository:
        @asynccontextmanager
        async def stream_export(self, *_args: object, **_kwargs: object) -> AsyncIterator[Any]:
            async def partitions() -> AsyncIterator[Any]:
                raise RuntimeError("query failed")
                yield []

            yield SimpleNamespace(partitions=partitions)

    with pytest.raises(RuntimeError, match="query failed"):
        await export_module.export_txt(
            ActivityFilter(),
            repository=cast(ActivityQueryRepository, FailingRepository()),
            logs_dir=tmp_path,
            scope=ActivityScope(frozenset(), all_tenants=True),
        )
    assert list((tmp_path / "system" / "tmp").iterdir()) == []


@pytest.mark.asyncio
async def test_export_service_records_delivery_failure_payload(tmp_path: Path) -> None:
    calls: list[Any] = []

    class Audit:
        async def record(self, _session: object, event: object) -> None:
            calls.append(event)

    service = export_module.ExportService(
        cast(ActivityQueryRepository, object()),
        Audit(),
        cast(AsyncSession, object()),
        tmp_path,
    )
    error = ValueError("send failed")
    await service.record_delivery_failure(42, error)
    event = calls[0]
    assert event.result == "failure"
    assert event.payload == {"error": "ValueError", "phase": "delivery"}


@pytest.mark.asyncio
async def test_export_service_records_success_after_delivery(tmp_path: Path) -> None:
    calls: list[Any] = []

    class Audit:
        async def record(self, _session: object, event: object) -> None:
            calls.append(event)

    service = export_module.ExportService(
        cast(ActivityQueryRepository, object()),
        Audit(),
        cast(AsyncSession, object()),
        tmp_path,
    )
    await service.record_delivery_success(42)
    assert [(event.action, event.result) for event in calls] == [("activity_export", "success")]
