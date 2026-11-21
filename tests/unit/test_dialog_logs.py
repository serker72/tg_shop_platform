from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.modules.iam.snapshot import PermissionSnapshot
from app.modules.logging.presentation.tg_master import logs as logs_module
from app.modules.logging.presentation.tg_master.logs import (
    FIELD_TITLES,
    FILTER_FIELDS,
    _field_titles,
    _parse_value,
    logs_dialog,
)
from app.modules.logging.query import ActivityQueryRepository, ActivityRow, Page
from tests.unit._master_bot_env import SUPER, Env


def test_logs_dialog_exposes_every_filter_control() -> None:
    dialog = logs_dialog()
    assert dialog is not None
    assert set(FILTER_FIELDS) == {
        "user_id",
        "telegram_user_id",
        "username",
        "bot_id",
        "tenant_id",
        "start_at",
        "end_at",
        "action_type",
        "entity_type",
        "entity_id",
        "product_id",
        "category_id",
        "subcategory_id",
        "order_id",
        "case_id",
        "preorder_id",
        "moderator",
    }


def test_filter_titles_use_russian_msgids_and_translate_at_runtime() -> None:
    assert set(FIELD_TITLES) == set(FILTER_FIELDS)
    assert FIELD_TITLES["user_id"] == "Пользователь"
    assert FIELD_TITLES["start_at"] == "Начало периода"
    assert all("User Id" not in title for title in _field_titles().values())


def test_filter_parser_validates_uuid_and_timezone() -> None:
    value = uuid4()
    assert _parse_value("tenant_id", str(value)) == value
    parsed = _parse_value("start_at", "2026-01-01T00:00:00+00:00")
    assert isinstance(parsed, datetime) and parsed.tzinfo is UTC
    with pytest.raises(ValueError):
        _parse_value("tenant_id", "not-a-uuid")
    with pytest.raises(ValueError):
        _parse_value("start_at", "2026-01-01")


def test_filter_parser_rejects_forged_unknown_field() -> None:
    with pytest.raises(ValueError):
        _parse_value("forged_callback", "1")


@pytest.mark.asyncio
async def test_forged_filter_callback_rechecks_permission_and_is_rejected() -> None:
    callback: Any = SimpleNamespace(answer=AsyncMock())
    button: Any = SimpleNamespace(widget_id="forged_callback")
    manager: Any = SimpleNamespace(dialog_data={})
    with patch.object(logs_module, "_require", new=AsyncMock()) as require:
        await logs_module._select_filter(callback, button, manager)
    require.assert_awaited_once_with(manager)
    callback.answer.assert_awaited_once_with("Некорректный фильтр", show_alert=True)
    assert manager.dialog_data == {}


@pytest.mark.asyncio
async def test_navigation_and_sort_recheck_permission_and_reset_cursor() -> None:
    callback: Any = SimpleNamespace(answer=AsyncMock())
    button: Any = SimpleNamespace()
    manager: Any = SimpleNamespace(
        dialog_data={"next": "cursor-2", "cursor": "cursor-1", "stack": []},
        show=AsyncMock(),
    )
    with patch.object(logs_module, "_require", new=AsyncMock()) as require:
        await logs_module._next(callback, button, manager)
        await logs_module._sort(callback, button, manager)
    assert require.await_count == 2
    assert manager.dialog_data["order"] == "oldest"
    assert manager.dialog_data["stack"] == []
    assert "cursor" not in manager.dialog_data
    assert manager.show.await_count == 2


@pytest.mark.asyncio
async def test_logs_dialog_real_botclient_lifecycle(tmp_path: Path) -> None:
    first_id, second_id = uuid4(), uuid4()
    first_row = SimpleNamespace(
        id=first_id, created_at=datetime(2026, 1, 1, tzinfo=UTC), telegram_bot_id=1
    )
    second_row = SimpleNamespace(
        id=second_id, created_at=datetime(2026, 1, 1, tzinfo=UTC), telegram_bot_id=1
    )

    class Repository:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def search(
            self, _filter: Any, _order: str, cursor: Any = None, **kwargs: Any
        ) -> Page:
            self.calls.append({"cursor": cursor, **kwargs})
            if kwargs.get("backward"):
                return Page([ActivityRow(cast(Any, first_row))], None, None, False)
            if cursor is not None:
                return Page([ActivityRow(cast(Any, second_row))], None, None, False)
            return Page(
                [ActivityRow(cast(Any, first_row))],
                f"2026-01-01T00:00:00+00:00|{first_id}",
                None,
                True,
            )

        @staticmethod
        def decode_cursor(value: str) -> tuple[datetime, Any]:
            return ActivityQueryRepository.decode_cursor(value)

    class Export:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def export_txt(self, *args: Any, **kwargs: Any) -> Path:
            self.calls.append({"args": args, "kwargs": kwargs})
            path = tmp_path / "logs.txt"
            path.write_text("log", encoding="utf-8")
            return path

        async def record_delivery_success(self, _actor: int) -> None:
            return None

        async def record_delivery_failure(self, _actor: int, _error: Exception) -> None:
            return None

    repository = Repository()
    exporter = Export()
    env = Env(SUPER, activity_repository=repository, export_service=exporter)
    await env.start()
    assert "Логи" in env.buttons()
    await env.press("Логи")
    assert env.messages.last_message().text is not None
    baseline = len(env.permissions.require_calls)
    assert baseline >= 1

    await env.press("→")
    after_next = len(env.permissions.require_calls)
    assert after_next > baseline
    await env.press("←")
    after_back = len(env.permissions.require_calls)
    assert after_back > after_next
    await env.press("Сортировка")
    after_sort = len(env.permissions.require_calls)
    assert after_sort > after_back
    assert any(call.get("backward") for call in repository.calls)

    await env.forge("export", "export", "forged")
    assert not exporter.calls

    send_document = AsyncMock()
    with patch.object(env.client.bot, "send_document", new=send_document):
        await env.press("Экспорт TXT")
    assert len(exporter.calls) == 1
    send_document.assert_awaited_once()
    assert len(env.permissions.require_calls) > after_sort


@pytest.mark.asyncio
async def test_export_sends_document_and_rechecks_permission(tmp_path: Path) -> None:
    exported = tmp_path / "logs.txt"
    exported.write_text("log", encoding="utf-8")
    service: Any = SimpleNamespace(
        export_txt=AsyncMock(return_value=exported),
        record_delivery_failure=AsyncMock(),
        record_delivery_success=AsyncMock(),
    )
    callback: Any = SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=42)))
    manager: Any = SimpleNamespace(
        dialog_data={},
        middleware_data={
            "bot": AsyncMock(),
            "permission_snapshot": PermissionSnapshot(user_id=None, is_superuser=True),
        },
    )
    button: Any = SimpleNamespace()
    with (
        patch.object(logs_module, "_require", new=AsyncMock()) as require,
        patch.object(logs_module, "resolve", new=AsyncMock(return_value=service)),
        patch.object(logs_module, "actor_id", return_value=7),
        patch.object(logs_module, "_filter", return_value=object()),
    ):
        await logs_module._export(callback, button, manager)
    require.assert_awaited_once_with(manager)
    manager.middleware_data["bot"].send_document.assert_awaited_once()
    assert manager.middleware_data["bot"].send_document.await_args.kwargs["chat_id"] == 42
    service.record_delivery_success.assert_awaited_once_with(7)
    assert not exported.exists()
