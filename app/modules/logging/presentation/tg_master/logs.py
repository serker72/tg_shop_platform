"""Master Bot activity viewer with server-side validation on every operation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from aiogram import Router
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Column, Row
from aiogram_dialog.widgets.text import Const, Format

from app.core.i18n import gettext as _
from app.modules.iam.permissions import LOGS_VIEW, TENANTS_VIEW_ALL
from app.modules.iam.presentation.tg_master.common import snapshot_of
from app.modules.iam.service import PermissionService
from app.modules.logging.export import ExportService
from app.modules.logging.query import ActivityFilter, ActivityQueryRepository, ActivityScope
from app.shared_kernel.presentation.tg_master.common import actor_id, resolve

from .states import LogsSG

FILTER_FIELDS = (
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
)
FIELD_TITLES = {
    "user_id": "Пользователь",
    "telegram_user_id": "Telegram ID пользователя",
    "username": "Username",
    "bot_id": "Бот",
    "tenant_id": "Магазин",
    "start_at": "Начало периода",
    "end_at": "Конец периода",
    "action_type": "Тип действия",
    "entity_type": "Тип сущности",
    "entity_id": "ID сущности",
    "product_id": "Товар",
    "category_id": "Категория",
    "subcategory_id": "Подкатегория",
    "order_id": "Заказ",
    "case_id": "Обращение",
    "preorder_id": "Предзаказ",
    "moderator": "Модератор",
}


def _field_titles() -> dict[str, str]:
    """Translate labels at render/callback time, inside the active locale context."""
    return {field: _(msgid) for field, msgid in FIELD_TITLES.items()}


INT_FIELDS = {"telegram_user_id", "bot_id", "moderator"}
UUID_FIELDS = {
    "user_id",
    "tenant_id",
    "entity_id",
    "product_id",
    "category_id",
    "subcategory_id",
    "order_id",
    "case_id",
    "preorder_id",
}
TEXT_FIELDS = {"username", "action_type", "entity_type"}
DATETIME_FIELDS = {"start_at", "end_at"}


def _scope(manager: DialogManager) -> ActivityScope:
    snapshot = snapshot_of(manager)
    master_id = manager.middleware_data.get("master_telegram_bot_id")
    return ActivityScope(
        tenant_ids=frozenset(snapshot.tenants),
        master_telegram_bot_id=master_id if isinstance(master_id, int) and master_id > 0 else None,
        all_tenants=snapshot.decide(TENANTS_VIEW_ALL).allowed,
    )


def _filter(manager: DialogManager) -> ActivityFilter:
    values = dict(manager.dialog_data.get("filter", {}))
    master_id = manager.middleware_data.get("master_telegram_bot_id")
    if isinstance(master_id, int) and master_id > 0:
        values["master_telegram_bot_id"] = master_id
    for field in UUID_FIELDS:
        if isinstance(values.get(field), str):
            values[field] = UUID(values[field])
    for field in DATETIME_FIELDS:
        if isinstance(values.get(field), str):
            values[field] = datetime.fromisoformat(values[field])
    now = datetime.now(UTC)
    values.setdefault("start_at", now - timedelta(hours=24))
    values.setdefault("end_at", now)
    result = ActivityFilter(**values)
    if result.start_at >= result.end_at:
        raise ValueError("time range must be increasing")
    return result


def _cursor(manager: DialogManager) -> tuple[datetime, UUID] | None:
    value = manager.dialog_data.get("cursor")
    if not isinstance(value, str):
        return None
    try:
        return ActivityQueryRepository.decode_cursor(value)
    except (ValueError, TypeError):
        raise ValueError("invalid cursor") from None


async def _require(manager: DialogManager) -> None:
    await (await resolve(manager, PermissionService)).require(actor_id(manager), LOGS_VIEW)


async def logs_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    await _require(dialog_manager)
    repository = await resolve(dialog_manager, ActivityQueryRepository)
    page = await repository.search(
        _filter(dialog_manager),
        dialog_manager.dialog_data.get("order", "newest"),
        _cursor(dialog_manager),
        scope=_scope(dialog_manager),
        backward=bool(dialog_manager.dialog_data.get("backward", False)),
    )
    was_backward = bool(dialog_manager.dialog_data.get("backward", False))
    dialog_manager.dialog_data["next"] = page.next_cursor
    if was_backward and not dialog_manager.dialog_data.get("stack"):
        dialog_manager.dialog_data.pop("cursor", None)
    dialog_manager.dialog_data["first"] = (
        f"{page.items[0].activity.created_at.isoformat()}|{page.items[0].activity.id}"
        if page.items
        else None
    )
    dialog_manager.dialog_data.pop("backward", None)
    return {
        "title": _("Логи активности"),
        "rows": "\n".join(str(row.activity.id) for row in page.items),
        "has_next": bool(page.next_cursor),
        "has_prev": bool(dialog_manager.dialog_data.get("stack")),
        "filter_hint": _("Диапазон по умолчанию: последние 24 часа"),
        **{f"filter_{field}": title for field, title in _field_titles().items()},
    }


async def _select_master_bot(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    await _require(manager)
    master_id = manager.middleware_data.get("master_telegram_bot_id")
    if not isinstance(master_id, int) or master_id <= 0:
        await callback.answer(_("Master Bot недоступен"), show_alert=True)
        return
    values = dict(manager.dialog_data.get("filter", {}))
    values["master_telegram_bot_id"] = master_id
    values["bot_id"] = master_id
    manager.dialog_data["filter"] = values
    manager.dialog_data.pop("cursor", None)
    manager.dialog_data["stack"] = []
    await callback.answer(_("Master Bot выбран"))


async def _select_filter(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _require(manager)
    field = str(button.widget_id)
    if field not in FILTER_FIELDS:
        await callback.answer(_("Некорректный фильтр"), show_alert=True)
        return
    manager.dialog_data["pending_filter"] = field
    await callback.answer(_("Введите значение: {field}").format(field=_field_titles()[field]))


def _parse_value(field: str, raw: str) -> object:
    value = raw.strip()
    if not value:
        raise ValueError("empty value")
    if field in INT_FIELDS:
        if not value.isascii() or not value.isdecimal() or int(value) <= 0:
            raise ValueError("invalid integer")
        return int(value)
    if field in UUID_FIELDS:
        return UUID(value)
    if field in DATETIME_FIELDS:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("datetime must include timezone")
        return parsed.astimezone(UTC)
    if field in TEXT_FIELDS:
        return value[1:] if field == "username" and value.startswith("@") else value
    raise ValueError("unknown filter")


async def _input(message: Message, widget: MessageInput, manager: DialogManager) -> None:
    await _require(manager)
    field = manager.dialog_data.get("pending_filter")
    if not isinstance(field, str) or field not in FILTER_FIELDS:
        await message.answer(_("Сначала выберите фильтр"))
        return
    try:
        parsed = _parse_value(field, message.text or "")
        values = dict(manager.dialog_data.get("filter", {}))
        values[field] = str(parsed) if field in UUID_FIELDS | DATETIME_FIELDS else parsed
        if field in DATETIME_FIELDS:
            start = values.get("start_at")
            end = values.get("end_at")
            if start and end and start >= end:
                raise ValueError("start_at must be before end_at")
        manager.dialog_data["filter"] = values
        manager.dialog_data.pop("pending_filter", None)
        manager.dialog_data.pop("cursor", None)
        manager.dialog_data["stack"] = []
        await message.answer(_("Фильтр сохранён"))
    except (ValueError, TypeError):
        await message.answer(_("Некорректное значение фильтра"))


async def _next(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _require(manager)
    value = manager.dialog_data.get("next")
    if not isinstance(value, str):
        await callback.answer()
        return
    stack = manager.dialog_data.setdefault("stack", [])
    if not isinstance(stack, list):
        raise ValueError("invalid cursor stack")
    stack.append(manager.dialog_data.get("cursor"))
    manager.dialog_data["cursor"] = value
    manager.dialog_data.pop("backward", None)
    await manager.show()


async def _back(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _require(manager)
    stack = manager.dialog_data.get("stack", [])
    if not isinstance(stack, list) or not stack:
        await callback.answer()
        return
    first = manager.dialog_data.get("first")
    if not isinstance(first, str):
        await callback.answer()
        return
    ActivityQueryRepository.decode_cursor(first)
    stack.pop()
    manager.dialog_data["cursor"] = first
    manager.dialog_data["backward"] = True
    await manager.show()


async def _sort(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _require(manager)
    manager.dialog_data["order"] = (
        "oldest" if manager.dialog_data.get("order", "newest") == "newest" else "newest"
    )
    manager.dialog_data.pop("cursor", None)
    manager.dialog_data["stack"] = []
    await manager.show()


async def _export(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _require(manager)
    service = await resolve(manager, ExportService)
    path = None
    try:
        path = await service.export_txt(
            actor_id(manager),
            _filter(manager),
            manager.dialog_data.get("order", "newest"),
            scope=_scope(manager),
        )
        try:
            message = callback.message
            if message is None:
                raise ValueError("callback has no message")
            await manager.middleware_data["bot"].send_document(
                chat_id=message.chat.id, document=FSInputFile(path), caption=_("Экспорт логов")
            )
        except Exception as error:
            await service.record_delivery_failure(actor_id(manager), error)
            raise
        await service.record_delivery_success(actor_id(manager))
    finally:
        if path is not None:
            await asyncio.to_thread(path.unlink, missing_ok=True)


def logs_dialog() -> Dialog:
    filter_buttons = [
        Button(Format(f"{{filter_{field}}}"), id=field, on_click=_select_filter)
        for field in FILTER_FIELDS
    ]
    return Dialog(
        Window(
            Format("{title}\n{filter_hint}\n{rows}"),
            Column(
                Button(Const(_("Master Bot")), id="master_bot", on_click=_select_master_bot),
                *filter_buttons,
            ),
            MessageInput(_input),
            Row(
                Button(Const("←"), id="back", on_click=_back),
                Button(Const("→"), id="next", on_click=_next),
            ),
            Row(
                Button(Const(_("Сортировка")), id="sort", on_click=_sort),
                Button(Const(_("Экспорт TXT")), id="export", on_click=_export),
            ),
            state=LogsSG.list,
            getter=logs_getter,
        )
    )


def build_logs_router() -> Router:
    router = Router(name="logging.logs")
    router.include_router(logs_dialog())
    return router


__all__ = ["FILTER_FIELDS", "LogsSG", "build_logs_router", "logs_dialog"]
