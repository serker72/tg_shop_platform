"""Master Bot activity viewer: draft edits never change applied queries."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import Any, cast
from uuid import UUID

from aiogram import Router
from aiogram.enums import ContentType, ParseMode
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Column, Row, Select
from aiogram_dialog.widgets.text import Format

from app.core.i18n import gettext as _
from app.modules.iam.permissions import LOGS_VIEW, TENANTS_VIEW_ALL
from app.modules.iam.service import PermissionService
from app.modules.logging.export import ExportService
from app.modules.logging.filter_options import OPTION_PAGE_SIZE, ActivityFilterOptions, OptionKind
from app.modules.logging.query import (
    UI_PAGE_SIZE,
    ActivityFilter,
    ActivityQueryRepository,
    ActivityRow,
    ActivityScope,
    Order,
)
from app.modules.logging.time_display import project_for_display
from app.shared_kernel.presentation.tg_master.common import actor_id, resolve
from app.shared_kernel.timezones import (
    AmbiguousLocalTimeError,
    NonexistentLocalTimeError,
    to_utc,
)

from .states import LogsSG

CATEGORIES = {
    "bots": ("shop", "bot", "bot_kind"),
    "users": ("staff", "visitor", "uid", "username"),
    "period": ("period", "start_at", "end_at"),
    "actions": ("action_type",),
    "moderators": ("moderator",),
}
CHOICES = {"shop", "bot", "staff", "visitor", "moderator"}
EXAMPLES = {
    "uid": "123456789",
    "username": "@example",
    "start_at": "2026-11-21 00:00",
    "end_at": "2026-11-22 00:00",
    "action_type": "command",
}
PERIODS = {"24h": 1, "7d": 7, "30d": 30}
DATE_FIELDS = ("start_at", "end_at")


@dataclass(frozen=True, slots=True)
class StaffTimezoneReader:
    read: Callable[[int], Awaitable[str]]


async def _staff_timezone(manager: DialogManager) -> str:
    reader = await resolve(manager, StaffTimezoneReader)
    return await reader.read(actor_id(manager))


def _titles() -> dict[str, str]:
    return {
        "bots": _("Боты"),
        "users": _("Пользователи"),
        "period": _("Период"),
        "actions": _("Действие"),
        "moderators": _("Модератор"),
        "shop": _("Магазин"),
        "bot": _("Бот магазина"),
        "master": _("Master Bot"),
        "staff": _("Сотрудник"),
        "visitor": _("Посетитель"),
        "uid": _("Telegram ID"),
        "username": _("Username"),
        "start_at": _("Начало периода"),
        "end_at": _("Конец периода"),
        "action_type": _("Тип действия"),
        "moderator": _("Модератор"),
    }


def _bounded(value: object, limit: int) -> str:
    text = " ".join(str(value).split()).replace("|", "/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _html(value: object, limit: int, encoded_limit: int) -> str:
    parts: list[str] = []
    size = 0
    for char in _bounded(value, limit):
        token = escape(char)
        if size + len(token) > encoded_limit - 1:
            parts.append("…")
            break
        size += len(token)
        parts.append(token)
    return "".join(parts)


def _line(row: ActivityRow, master: int | None, timezone: str) -> str:
    a = row.activity
    bot = _("Master") if a.telegram_bot_id == master else row.bot_name or a.telegram_bot_id
    user = ("@" + a.username.lstrip("@")) if a.username else a.telegram_user_id or "—"
    action = f"cmd: {a.button}" if a.action_type == "command" and a.button else a.action_type
    shown_at, utc_fallback = project_for_display(a.created_at, timezone)
    timestamp = (
        f"{shown_at.isoformat(sep=' ')} UTC" if utc_fallback else shown_at.strftime("%d.%m %H:%M")
    )
    return " | ".join(
        (
            timestamp,
            _html(bot, 32, 64),
            _html(user, 32, 64),
            _html(action, 48, 88),
        )
    )


async def _require(manager: DialogManager) -> ActivityScope:
    actor = actor_id(manager)
    permissions = await resolve(manager, PermissionService)
    await permissions.require(actor, LOGS_VIEW)
    snapshot = await permissions.get_snapshot(actor)
    master = manager.middleware_data.get("master_telegram_bot_id")
    return ActivityScope(
        frozenset(snapshot.tenants),
        master if type(master) is int and master > 0 else None,
        snapshot.decide(TENANTS_VIEW_ALL).allowed,
        actor if actor > 0 else None,
    )


def _utc_bound(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("expected an aware period bound")
    return parsed.astimezone(UTC)


def _dates(values: dict[str, Any]) -> tuple[datetime, datetime]:
    start, end = (_utc_bound(values[key]) for key in DATE_FIELDS)
    if start >= end:
        raise ValueError("invalid period")
    return start, end


def _freeze(values: dict[str, Any]) -> dict[str, Any]:
    period = values.get("period", "24h")
    if period == "custom":
        start, end = _dates(values)
    else:
        end = datetime.now(UTC)
        start = end - timedelta(days=PERIODS[period])
    return {**values, "start_at": start.isoformat(), "end_at": end.isoformat()}


def _applied(manager: DialogManager) -> dict[str, Any]:
    if "applied" not in manager.dialog_data:
        manager.dialog_data["applied"] = _freeze({})
    return cast(dict[str, Any], manager.dialog_data["applied"])


def _draft(manager: DialogManager) -> dict[str, Any]:
    return cast(dict[str, Any], manager.dialog_data.setdefault("draft", {}))


def _nav_reset(data: dict[str, Any]) -> None:
    for key in ("cursor", "backward", "next", "prev", "page", "reset_page"):
        data.pop(key, None)


async def _shop_scope(
    values: dict[str, Any], options: ActivityFilterOptions, scope: ActivityScope
) -> UUID | None:
    if values.get("bot_kind") == "master":
        return None
    for kind in ("shop", "bot"):
        if kind in values:
            option = await options.get(cast(OptionKind, kind), scope, None, values[kind])
            if option is None:
                raise ValueError("invalid bot/shop")
            return option.tenant_id
    return None


async def _filter(
    manager: DialogManager, values: dict[str, Any], scope: ActivityScope
) -> tuple[ActivityFilter, dict[str, str]]:
    kwargs: dict[str, Any] = {}
    labels: dict[str, str] = {}
    visitor_tenant = UUID(values["visitor_tenant"]) if values.get("visitor") else None
    bot_tenant = None
    for kind, field in (
        ("shop", "tenant_id"),
        ("bot", "bot_id"),
        ("staff", "user_id"),
        ("visitor", "visitor"),
        ("moderator", "moderator"),
    ):
        if kind not in values:
            continue
        options = await resolve(manager, ActivityFilterOptions)
        option = await options.get(
            cast(OptionKind, kind),
            scope,
            visitor_tenant if kind == "visitor" else None,
            values[kind],
        )
        if option is None:
            raise ValueError("invalid selection")
        labels[kind] = option.label
        if kind == "visitor":
            if visitor_tenant is None or option.tenant_id != visitor_tenant:
                raise ValueError("invalid visitor tenant")
            shop = await options.get("shop", scope, None, str(visitor_tenant))
            if shop is None:
                raise ValueError("invalid visitor shop")
            labels["visitor_shop"] = shop.label
            kwargs[field] = (visitor_tenant, int(option.key))
        else:
            kwargs[field] = UUID(option.key) if kind in ("shop", "staff") else int(option.key)
        if kind == "bot":
            bot_tenant = option.tenant_id
    if values.get("bot_kind") == "master":
        if scope.master_telegram_bot_id is None or (
            not scope.all_tenants and scope.actor_telegram_id is None
        ):
            raise ValueError("Master is unavailable")
        kwargs["bot_id"] = scope.master_telegram_bot_id
        labels["bot_kind"] = _("Master Bot")
    for key, field in (
        ("uid", "telegram_user_id"),
        ("username", "username"),
        ("action_type", "action_type"),
    ):
        if key in values:
            labels[key] = str(values[key])
            kwargs[field] = values[key]
    if visitor_tenant is not None and bot_tenant is not None:
        # A shop bot selection has a tenant too; do not match inconsistent historical rows.
        kwargs["tenant_ids"] = frozenset({bot_tenant})
    if visitor_tenant is not None and (
        (bot_tenant is not None and visitor_tenant != bot_tenant)
        or ("tenant_id" in kwargs and visitor_tenant != kwargs["tenant_id"])
    ):
        labels["conflict"] = _(
            "Посетитель и выбранный бот или магазин относятся к разным магазинам: "
            "пересечение пусто."
        )
    start, end = _dates(values)
    return ActivityFilter(start_at=start, end_at=end, **kwargs), labels


def _local_bound(value: str, timezone: str) -> str:
    shown_at, utc_fallback = project_for_display(_utc_bound(value), timezone)
    return shown_at.isoformat(sep=" ") + (" UTC" if utc_fallback else "")


def _timezone_label(timezone: str) -> str:
    return _("Часовой пояс: {timezone}").format(timezone=timezone)


def _summary(values: dict[str, Any], labels: dict[str, str], timezone: str) -> str:
    titles = _titles()
    period = values.get("period", "24h")
    bounds = " – ".join(_local_bound(values[key], timezone) for key in DATE_FIELDS)
    if period != "custom":
        preset = {"24h": _("24 ч"), "7d": _("7 д"), "30d": _("30 д")}[period]
        bounds = f"{preset} · {bounds}"
    titles.update(bot_kind=titles["bots"], visitor_shop=_("Магазин посетителя"))
    lines = [escape(_timezone_label(timezone)), f"{escape(titles['period'])}: {escape(bounds)}"]
    lines.extend(
        f"{escape(titles[key])}: {_html(value, 44, 90)}"
        for key, value in labels.items()
        if key != "conflict"
    )
    if "conflict" in labels:
        lines.append(escape(labels["conflict"]))
    return "\n".join(lines)


def _buttons() -> dict[str, str]:
    return {
        **_titles(),
        "filters": _("Фильтры"),
        "export": _("Экспорт TXT"),
        "close": _("В меню"),
        "cancel": _("Отмена"),
        "apply": _("Применить"),
        "clear": _("Очистить"),
        "return": _("Назад"),
        "24h": _("24 ч"),
        "7d": _("7 д"),
        "30d": _("30 д"),
        "prev": "←",
        "next": "→",
    }


def _order(data: dict[str, Any]) -> Order:
    return cast(Order, data.get("order", "newest"))


def _opposite_order(order: Order) -> Order:
    return "oldest" if order == "newest" else "newest"


def _order_label(order: Order) -> str:
    if order == "newest":
        return _("Порядок по времени события: сначала новые")
    return _("Порядок по времени события: сначала старые")


def _sort_action(target_order: Order) -> str:
    return _("Показать сначала новые") if target_order == "newest" else _("Показать сначала старые")


async def logs_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    manager = dialog_manager
    scope = await _require(manager)
    data = manager.dialog_data
    values = _applied(manager)
    timezone = await _staff_timezone(manager)
    order = _order(data)
    header = f"<b>{_title()}</b>\n{escape(_order_label(order))}\n"
    sort_action = _sort_action(_opposite_order(order))
    try:
        f, labels = await _filter(manager, values, scope)
    except ValueError:
        _nav_reset(data)
        return {
            **_buttons(),
            "screen": header + escape(_invalid()) + "\n0/0",
            "has_prev": False,
            "has_next": False,
            "can_export": False,
            "sort": sort_action,
        }
    repository = await resolve(manager, ActivityQueryRepository)
    page_no = data.get("page", 1)
    cursor = data.get("cursor")
    backward = data.get("backward", False)
    page = await repository.search(
        f,
        order,
        repository.decode_cursor(cursor) if cursor else None,
        scope=scope,
        backward=backward,
    )
    incomplete = 0 < len(page.items) < min(UI_PAGE_SIZE, page.total)
    if cursor and backward and page_no == 1 and (page.previous_cursor or incomplete):
        # Inserts or deletions can make the backward window differ from the first page.
        _nav_reset(data)
        cursor = None
        page = await repository.search(f, order, scope=scope)
    pages = (page.total + UI_PAGE_SIZE - 1) // UI_PAGE_SIZE
    stale = bool(cursor and (not page.items or page_no > pages))
    data.update(
        next=page.next_cursor, prev=page.previous_cursor if page_no > 1 else None, reset_page=stale
    )
    if stale:
        data.update(next=None, prev=None)
    rows = "\n".join(_line(row, scope.master_telegram_bot_id, timezone) for row in page.items) or _(
        "Нет событий"
    )
    screen = f"{header}{_summary(values, labels, timezone)}\n\n"
    screen += f"{min(page_no, pages)}/{pages}\n{rows}"
    if stale:
        screen += "\n" + escape(_("Список изменился. Вернитесь на первую страницу."))
    return {
        **_buttons(),
        "screen": screen,
        "can_export": True,
        "sort": sort_action,
        "has_next": bool(data["next"]),
        "has_prev": bool(data["prev"]) or stale,
    }


def _title() -> str:
    return escape(_("Логи активности"))


def _invalid() -> str:
    return _("Фильтр недоступен или содержит неверное значение")


async def _editor_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    manager = dialog_manager
    scope = await _require(manager)
    values = _draft(manager)
    data = manager.dialog_data
    state = manager.current_context().state
    buttons = _buttons()
    category = data.get("category")
    result: dict[str, Any] = {**buttons}
    if state == LogsSG.filters:
        for key, fields in CATEGORIES.items():
            if key == "period" or any(field in values for field in fields):
                result[key] = "✓ " + buttons[key]
        result["screen"] = (
            "<b>"
            + escape(_("Фильтры логов"))
            + "</b>\n"
            + escape(_("Изменения вступят в силу после применения"))
        )
        return result
    if state == LogsSG.input:
        field = data["field"]
        current = data.get("input_period", {}).get(field, values.get(field))
        timezone = await _staff_timezone(manager) if field in DATE_FIELDS else None
        if timezone is not None and current is not None:
            current = _local_bound(current, timezone)
        result["screen"] = escape(
            _("Введите {field}\nТекущее: {current}\nПример: {example}").format(
                field=_titles()[field],
                current=_bounded(current, 60) if current is not None else "—",
                example=EXAMPLES[field],
            )
        )
        if timezone is not None:
            result["screen"] += "\n" + escape(_timezone_label(timezone))
            result["screen"] += "\n" + escape(
                _("Без смещения используется этот часовой пояс; можно указать смещение или Z.")
            )
        return result
    tenant = None
    if "shop" in values or "bot" in values:
        options = await resolve(manager, ActivityFilterOptions)
        try:
            tenant = await _shop_scope(values, options, scope)
        except ValueError:
            pass
    if state == LogsSG.choices:
        options = await resolve(manager, ActivityFilterOptions)
        kind = data["kind"]
        owner = _choice_tenant(data, values, tenant)
        offset = data.get("offset", 0)
        rows, more = await options.page(kind, scope, owner, offset)
        if not rows and offset:
            data["offset"] = offset = 0
            rows, more = await options.page(kind, scope, owner, offset)
        result.update(
            items=[(option.key, _bounded(option.label, 48)) for option in rows],
            choice_prev=offset > 0,
            choice_next=more,
            screen=escape(_titles()[kind])
            + f" · {offset // OPTION_PAGE_SIZE + 1}"
            + ("" if rows else "\n" + escape(_("Нет вариантов"))),
        )
        data["choice_more"] = more
        return result
    if category not in CATEGORIES:
        raise ValueError("invalid category")
    result["screen"] = escape(_titles()[str(category)])
    try:
        frozen = _freeze(values)
        _validated, labels = await _filter(manager, frozen, scope)
        result["screen"] += "\n" + _summary(frozen, labels, await _staff_timezone(manager))
    except (ValueError, KeyError):
        result["screen"] += "\n" + escape(_invalid())
    result.update({f"cat_{key}": key == category for key in CATEGORIES})
    if category == "users" and tenant is not None:
        result["visitor"] = _("Посетители выбранного магазина")
    result["can_master"] = category == "bots" and scope.master_telegram_bot_id is not None
    return result


def _choice_tenant(data: dict[str, Any], draft: dict[str, Any], tenant: UUID | None) -> UUID | None:
    if data["kind"] == "visitor":
        if data.get("visitor_shop"):
            return UUID(data["visitor_shop"])
        return tenant
    if data["kind"] == "bot" and draft.get("shop"):
        # Constrain even if the selected shop was revoked after rendering.
        return UUID(draft["shop"])
    return None


def _parse_value(field: str, raw: str, timezone: str = "UTC") -> str | int:
    value = raw.strip()
    if field == "uid" and re.fullmatch(r"[0-9]{1,19}", value) and 0 < int(value) < 2**63:
        return int(value)
    if field == "username" and re.fullmatch(r"@?[A-Za-z0-9_]{1,32}", value):
        return value.removeprefix("@").lower()
    if field == "action_type" and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value):
        return value
    if field in DATE_FIELDS:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is not None:
            return parsed.astimezone(UTC).isoformat()
        try:
            date.fromisoformat(value)
        except ValueError:
            return to_utc(parsed, timezone).isoformat()
    raise ValueError("invalid filter value")


def _clear(values: dict[str, Any], category: str) -> None:
    for field in CATEGORIES[category]:
        values.pop(field, None)
    if category == "users":
        values.pop("visitor_tenant", None)


async def _input(message: Message, widget: MessageInput, manager: DialogManager) -> None:
    await _require(manager)
    field = manager.dialog_data["field"]
    timezone = await _staff_timezone(manager) if field in DATE_FIELDS else "UTC"
    try:
        value = _parse_value(field, message.text or "", timezone)
    except AmbiguousLocalTimeError:
        await message.answer(
            _("Неоднозначное местное время ({timezone}). Укажите числовое смещение UTC.").format(
                timezone=timezone
            )
        )
        return
    except NonexistentLocalTimeError:
        await message.answer(
            _("Такого местного времени нет ({timezone}). Выберите корректное время.").format(
                timezone=timezone
            )
        )
        return
    except (ValueError, OverflowError):
        await message.answer(_invalid())
        return
    draft = _draft(manager)
    if field in DATE_FIELDS:
        draft.update(manager.dialog_data.pop("input_period"))
        draft["period"] = "custom"
    draft[field] = value
    await manager.switch_to(LogsSG.category)


async def _main(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _require(manager)
    data = manager.dialog_data
    key = button.widget_id
    if key == "close":
        await manager.done()
    elif key == "filters":
        data["draft"] = dict(_applied(manager))
        await manager.switch_to(LogsSG.filters)
    elif key == "sort":
        data["order"] = _opposite_order(_order(data))
        _nav_reset(data)
    elif key == "prev" and data.get("reset_page"):
        _nav_reset(data)
    elif key in ("prev", "next") and data.get(key):
        data.update(
            cursor=data[key],
            backward=key == "prev",
            page=data.get("page", 1) + (-1 if key == "prev" else 1),
        )


async def _edit(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    scope = await _require(manager)
    data = manager.dialog_data
    draft = _draft(manager)
    key = button.widget_id
    state = manager.current_context().state
    if state == LogsSG.filters:
        if key == "apply":
            try:
                applied = _freeze(draft)
                await _filter(manager, applied, scope)
            except (ValueError, KeyError, OverflowError):
                await callback.answer(_invalid(), show_alert=True)
                return
            data["applied"] = applied
            _nav_reset(data)
        if key in ("apply", "cancel"):
            data.pop("draft", None)
            await manager.switch_to(LogsSG.list)
        elif key == "clear":
            data["draft"] = {}
        elif key in CATEGORIES:
            data["category"] = key
            await manager.switch_to(LogsSG.category)
        return
    if key == "return":
        data.pop("input_period", None)
        await manager.switch_to(LogsSG.filters if state == LogsSG.category else LogsSG.category)
        return
    category = data["category"]
    if state == LogsSG.choices:
        offset = data.get("offset", 0)
        if key == "prev" and offset:
            data["offset"] = max(0, offset - OPTION_PAGE_SIZE)
        if key == "next" and data.get("choice_more"):
            data["offset"] = offset + OPTION_PAGE_SIZE
        return
    if key == "clear":
        _clear(draft, category)
    elif category == "bots" and key == "master":
        if scope.master_telegram_bot_id is None or (
            not scope.all_tenants and scope.actor_telegram_id is None
        ):
            await callback.answer(_invalid(), show_alert=True)
            return
        _clear(draft, "bots")
        draft["bot_kind"] = "master"
    elif category == "period" and key in PERIODS:
        _clear(draft, "period")
        draft["period"] = key
    elif key in CATEGORIES[category] and key in EXAMPLES:
        data["field"] = key
        if key in DATE_FIELDS:
            # Preserve the other bound exactly as displayed, without mutating draft on Cancel.
            dates = draft if draft.get("period") == "custom" else _freeze(draft)
            data["input_period"] = {k: dates[k] for k in DATE_FIELDS}
        await manager.switch_to(LogsSG.input)
    elif key in CATEGORIES[category] and key in CHOICES:
        kind = key
        if key == "visitor":
            try:
                tenant = None
                if "shop" in draft or "bot" in draft:
                    options = await resolve(manager, ActivityFilterOptions)
                    tenant = await _shop_scope(draft, options, scope)
            except ValueError:
                await callback.answer(_invalid(), show_alert=True)
                return
            data["visitor_shop"] = str(tenant) if tenant else None
            if tenant is None:
                kind = "shop"
        data.update(kind=kind, offset=0, choice_more=False)
        await manager.switch_to(LogsSG.choices)


async def _choose(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: str
) -> None:
    scope = await _require(manager)
    data = manager.dialog_data
    draft = _draft(manager)
    kind = data["kind"]
    category = data["category"]
    options = await resolve(manager, ActivityFilterOptions)
    tenant = _choice_tenant(data, draft, None)
    option = await options.get(kind, scope, tenant, item_id)
    if option is None:
        await callback.answer(_invalid(), show_alert=True)
        return
    if kind == "shop" and category == "users":
        data.update(kind="visitor", visitor_shop=option.key, offset=0, choice_more=False)
        return
    if category != "users":
        _clear(draft, category)
    draft[kind] = option.key
    if kind == "visitor":
        draft["visitor_tenant"] = str(option.tenant_id)
    await manager.switch_to(LogsSG.category)


async def _export(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    scope = await _require(manager)
    try:
        f, _labels = await _filter(manager, _applied(manager), scope)
    except ValueError:
        await callback.answer(_invalid(), show_alert=True)
        return
    service = await resolve(manager, ExportService)
    actor = actor_id(manager)
    path = await service.export_txt(
        actor, f, _order(manager.dialog_data), scope=scope, timezone=await _staff_timezone(manager)
    )
    try:
        try:
            if callback.message is None:
                raise ValueError("callback has no message")
            await manager.middleware_data["bot"].send_document(
                chat_id=callback.message.chat.id,
                document=FSInputFile(path),
                caption=_("Экспорт логов"),
            )
        except Exception as error:
            await service.record_delivery_failure(actor, error)
            raise
        await service.record_delivery_success(actor)
    finally:
        await asyncio.to_thread(path.unlink, missing_ok=True)


def _button(key: str, *, main: bool = False, when: str | None = None) -> Button:
    handler = _export if key == "export" else _main if main else _edit
    return Button(Format("{" + key + "}"), id=key, on_click=handler, when=when)


def logs_dialog() -> Dialog:
    return Dialog(
        Window(
            Format("{screen}"),
            Row(
                _button("prev", main=True, when="has_prev"),
                _button("next", main=True, when="has_next"),
            ),
            Row(_button("filters", main=True), _button("sort", main=True)),
            Row(_button("export", main=True, when="can_export"), _button("close", main=True)),
            state=LogsSG.list,
            getter=logs_getter,
            parse_mode=ParseMode.HTML,
        ),
        Window(
            Format("{screen}"),
            Row(_button("bots"), _button("users")),
            Row(_button("period"), _button("actions")),
            Row(_button("moderators")),
            Row(_button("apply"), _button("cancel")),
            Row(_button("clear")),
            state=LogsSG.filters,
            getter=_editor_getter,
            parse_mode=ParseMode.HTML,
        ),
        Window(
            Format("{screen}"),
            Row(_button("shop"), _button("bot"), when="cat_bots"),
            Row(_button("master", when="can_master")),
            Row(_button("staff"), _button("visitor"), when="cat_users"),
            Row(_button("uid"), _button("username"), when="cat_users"),
            Row(_button("24h"), _button("7d"), _button("30d"), when="cat_period"),
            Row(_button("start_at"), _button("end_at"), when="cat_period"),
            Row(_button("action_type"), when="cat_actions"),
            Row(_button("moderator"), when="cat_moderators"),
            Row(_button("clear"), _button("return")),
            state=LogsSG.category,
            getter=_editor_getter,
            parse_mode=ParseMode.HTML,
        ),
        Window(
            Format("{screen}"),
            Column(
                Select(
                    Format("{item[1]}"),
                    id="pick",
                    item_id_getter=lambda item: item[0],
                    items="items",
                    on_click=_choose,
                )
            ),
            Row(_button("prev", when="choice_prev"), _button("next", when="choice_next")),
            _button("return"),
            state=LogsSG.choices,
            getter=_editor_getter,
            parse_mode=ParseMode.HTML,
        ),
        Window(
            Format("{screen}"),
            MessageInput(_input, content_types=ContentType.TEXT),
            Button(Format("{cancel}"), id="return", on_click=_edit),
            state=LogsSG.input,
            getter=_editor_getter,
            parse_mode=ParseMode.HTML,
        ),
    )


def build_logs_router() -> Router:
    router = Router(name="logging.logs")
    router.include_router(logs_dialog())
    return router


__all__ = ["LogsSG", "build_logs_router", "logs_dialog"]
