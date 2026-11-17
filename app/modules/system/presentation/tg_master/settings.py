"""Master Bot dialog "Settings" (``settings.manage``): maintenance, IAM cache TTL, retention.

``when`` is render-only: the getters re-check ``settings.manage`` on the server and every
handler re-checks it before switching windows; the service itself calls ``require``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiogram.enums import ContentType
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Cancel, Column, Row, SwitchTo
from aiogram_dialog.widgets.text import Format

from app.core.i18n import gettext as _
from app.modules.iam.permissions import SETTINGS_MANAGE
from app.modules.iam.presentation.tg_master.common import (
    actor_id,
    compose,
    denied_text,
    error_text,
    mutate,
    nav_texts,
    resolve,
    set_notice,
    snapshot_of,
)
from app.modules.system.models import (
    FILE_LOGS_RETENTION_DAYS_MAX,
    FILE_LOGS_RETENTION_DAYS_MIN,
    RETENTION_MONTHS_MAX,
    RETENTION_MONTHS_MIN,
)
from app.modules.system.service import (
    IAM_CACHE_TTL_SECONDS_MAX,
    IAM_CACHE_TTL_SECONDS_MIN,
    SystemSettingsService,
)
from app.shared_kernel.domain.exceptions import DomainError

__all__ = ["FIELDS", "SettingsSG", "parse_value", "settings_dialog"]

FIELD_KEY: Final = "field"
TARGET_KEY: Final = "maintenance"
#: ``dialog_data`` key of a pending retention decrease: ``{"field": "ua"|"audit", "value": N}``.
RETENTION_KEY: Final = "retention_decrease"
_RETENTION_FIELDS: Final = ("ua", "audit")
#: Editable numeric field -> (min, max).
FIELDS: Final[dict[str, tuple[int, int]]] = {
    "ttl": (IAM_CACHE_TTL_SECONDS_MIN, IAM_CACHE_TTL_SECONDS_MAX),
    "ua": (RETENTION_MONTHS_MIN, RETENTION_MONTHS_MAX),
    "audit": (RETENTION_MONTHS_MIN, RETENTION_MONTHS_MAX),
    "files": (FILE_LOGS_RETENTION_DAYS_MIN, FILE_LOGS_RETENTION_DAYS_MAX),
}


class SettingsSG(StatesGroup):
    main = State()
    confirm = State()
    edit = State()
    retention_confirm = State()


def parse_value(field: str, raw: str | None) -> int | None:
    """Integer of ``raw`` inside the range of ``field``; anything else -> ``None``.

    ASCII digits only: ``int()`` would also accept ``"1_000"``, ``"+5"`` and non-ASCII digits.
    """
    low, high = FIELDS[field]
    text = (raw or "").strip()
    if not (text.isascii() and text.isdecimal()):
        return None
    value = int(text)
    return value if low <= value <= high else None


def _allowed(manager: DialogManager) -> bool:
    return snapshot_of(manager).decide(SETTINGS_MANAGE).allowed


def _field_title(field: str) -> str:
    return {
        "ttl": _("TTL кэша прав (сек.)"),
        "ua": _("Хранение user_activity (мес.)"),
        "audit": _("Хранение audit_logs (мес.)"),
        "files": _("Хранение файловых логов (дн.)"),
    }[field]


def _texts() -> dict[str, Any]:
    return {
        **nav_texts(),
        **{f"btn_{field}": _field_title(field) for field in FIELDS},
    }


async def main_getter(dialog_manager: DialogManager, **_kw: Any) -> dict[str, Any]:
    data = _texts()
    if not _allowed(dialog_manager):
        return {**data, "allowed": False, "text": compose(dialog_manager, denied_text())}
    try:
        settings = await (await resolve(dialog_manager, SystemSettingsService)).get()
    except DomainError as error:
        return {**data, "allowed": False, "text": compose(dialog_manager, error_text(error.code))}
    on = bool(settings.is_maintenance_mode)
    lines = [
        _("Системные настройки"),
        "",
        _("Технические работы: {state}").format(state=_("включены") if on else _("выключены")),
        f"{_field_title('ttl')}: {settings.iam_cache_ttl_seconds}",
        f"{_field_title('ua')}: {settings.user_activity_retention_months}",
        f"{_field_title('audit')}: {settings.audit_logs_retention_months}",
        f"{_field_title('files')}: {settings.file_logs_retention_days}",
    ]
    return {
        **data,
        "allowed": True,
        "text": compose(dialog_manager, *lines),
        "btn_maintenance": _("Выключить техработы") if on else _("Включить техработы"),
    }


async def confirm_getter(dialog_manager: DialogManager, **_kw: Any) -> dict[str, Any]:
    data = _texts()
    if not _allowed(dialog_manager):
        return {**data, "allowed": False, "text": denied_text()}
    enabled = bool(dialog_manager.dialog_data.get(TARGET_KEY))
    question = (
        _("Включить режим технических работ? Боты будут отвечать только администраторам.")
        if enabled
        else _("Выключить режим технических работ?")
    )
    return {**data, "allowed": True, "text": question}


def _pending_retention(manager: DialogManager) -> tuple[str, int] | None:
    """Validated pending decrease from ``dialog_data``; anything malformed -> ``None``."""
    pending = manager.dialog_data.get(RETENTION_KEY)
    if not isinstance(pending, dict):
        return None
    field, value = pending.get("field"), pending.get("value")
    if field not in _RETENTION_FIELDS or isinstance(value, bool) or not isinstance(value, int):
        return None
    low, high = FIELDS[field]
    return (field, value) if low <= value <= high else None


async def retention_confirm_getter(dialog_manager: DialogManager, **_kw: Any) -> dict[str, Any]:
    data = _texts()
    pending = _pending_retention(dialog_manager)
    if not _allowed(dialog_manager) or pending is None:
        return {**data, "allowed": False, "text": denied_text()}
    field, value = pending
    question = (
        _("Партиции user_activity старше {n} мес. будут удалены безвозвратно. Продолжить?")
        if field == "ua"
        else _("Партиции audit_logs старше {n} мес. будут отсоединены и архивированы. Продолжить?")
    ).format(n=value)
    return {**data, "allowed": True, "text": question}


async def edit_getter(dialog_manager: DialogManager, **_kw: Any) -> dict[str, Any]:
    data = _texts()
    field = dialog_manager.dialog_data.get(FIELD_KEY)
    if not _allowed(dialog_manager) or field not in FIELDS:
        return {**data, "text": compose(dialog_manager, denied_text())}
    low, high = FIELDS[field]
    prompt = _("Введите новое значение «{title}» — целое число от {low} до {high}.").format(
        title=_field_title(field), low=low, high=high
    )
    return {**data, "text": compose(dialog_manager, prompt)}


async def _deny(callback: CallbackQuery) -> None:
    await callback.answer(denied_text())


async def _on_maintenance(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not _allowed(manager):
        await _deny(callback)
        return
    try:
        settings = await (await resolve(manager, SystemSettingsService)).get()
    except DomainError as error:
        await callback.answer(error_text(error.code))
        return
    manager.dialog_data[TARGET_KEY] = not bool(settings.is_maintenance_mode)
    await manager.switch_to(SettingsSG.confirm)


async def _on_confirm(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not _allowed(manager):
        await _deny(callback)
        return
    target = manager.dialog_data.pop(TARGET_KEY, None)
    if not isinstance(target, bool):
        # Repeated / forged "Yes" or lost FSM data: never guess the target state.
        set_notice(manager, _("Некорректное действие, повторите."))
        await manager.switch_to(SettingsSG.main)
        return
    enabled = target
    actor = actor_id(manager)
    await mutate(manager, SystemSettingsService, lambda s: s.set_maintenance(actor, enabled))
    await manager.switch_to(SettingsSG.main)


async def _on_edit(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not _allowed(manager):
        await _deny(callback)
        return
    manager.dialog_data[FIELD_KEY] = str(button.widget_id)
    await manager.switch_to(SettingsSG.edit)


def _setter(
    field: str, value: int, actor: int
) -> Callable[[SystemSettingsService], Awaitable[Any]]:
    if field == "ttl":
        return lambda s: s.set_iam_cache_ttl(actor, value)
    if field == "files":
        return lambda s: s.set_file_logs_retention_days(actor, value)
    if field == "ua":
        return lambda s: s.set_retention(actor, user_activity_months=value)
    return lambda s: s.set_retention(actor, audit_logs_months=value)


async def _on_input(message: Message, widget: MessageInput, manager: DialogManager) -> None:
    field = manager.dialog_data.get(FIELD_KEY)
    if not _allowed(manager) or field not in FIELDS:
        await message.answer(denied_text())
        return
    value = parse_value(field, message.text)
    if value is None:
        low, high = FIELDS[field]
        set_notice(
            manager,
            _("Некорректное значение: нужно целое число от {low} до {high}.").format(
                low=low, high=high
            ),
        )
        return
    if field in _RETENTION_FIELDS:
        try:
            current = await (await resolve(manager, SystemSettingsService)).get()
        except DomainError as error:
            set_notice(manager, error_text(error.code))
            return
        stored = (
            current.user_activity_retention_months
            if field == "ua"
            else current.audit_logs_retention_months
        )
        if value < stored:
            # Partitions are dropped / archived: ask before calling the service.
            manager.dialog_data[RETENTION_KEY] = {"field": field, "value": value}
            manager.dialog_data.pop(FIELD_KEY, None)
            await manager.switch_to(SettingsSG.retention_confirm)
            return
    await mutate(manager, SystemSettingsService, _setter(field, value, actor_id(manager)))
    manager.dialog_data.pop(FIELD_KEY, None)
    await manager.switch_to(SettingsSG.main)


async def _on_retention_confirm(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    if not _allowed(manager):
        manager.dialog_data.pop(RETENTION_KEY, None)
        await _deny(callback)
        return
    pending = _pending_retention(manager)
    manager.dialog_data.pop(RETENTION_KEY, None)
    if pending is None:
        # Repeated / forged "Yes" or lost FSM data: never guess the value.
        set_notice(manager, _("Некорректное действие, повторите."))
        await manager.switch_to(SettingsSG.main)
        return
    field, value = pending
    await mutate(manager, SystemSettingsService, _setter(field, value, actor_id(manager)))
    await manager.switch_to(SettingsSG.main)


async def _on_retention_cancel(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    manager.dialog_data.pop(RETENTION_KEY, None)
    await manager.switch_to(SettingsSG.main)


def settings_dialog() -> Dialog:
    return Dialog(
        Window(
            Format("{text}"),
            Column(
                Button(
                    Format("{btn_maintenance}"),
                    id="mnt",
                    on_click=_on_maintenance,
                    when="allowed",
                ),
                *(
                    Button(Format(f"{{btn_{field}}}"), id=field, on_click=_on_edit, when="allowed")
                    for field in FIELDS
                ),
            ),
            Cancel(Format("{btn_back}")),
            state=SettingsSG.main,
            getter=main_getter,
        ),
        Window(
            Format("{text}"),
            Row(
                Button(Format("{btn_yes}"), id="yes", on_click=_on_confirm, when="allowed"),
                SwitchTo(Format("{btn_no}"), id="no", state=SettingsSG.main),
            ),
            state=SettingsSG.confirm,
            getter=confirm_getter,
        ),
        Window(
            Format("{text}"),
            MessageInput(_on_input, content_types=[ContentType.TEXT]),
            SwitchTo(Format("{btn_back}"), id="back", state=SettingsSG.main),
            state=SettingsSG.edit,
            getter=edit_getter,
        ),
        Window(
            Format("{text}"),
            Row(
                Button(
                    Format("{btn_yes}"),
                    id="ret_yes",
                    on_click=_on_retention_confirm,
                    when="allowed",
                ),
                Button(Format("{btn_no}"), id="ret_no", on_click=_on_retention_cancel),
            ),
            state=SettingsSG.retention_confirm,
            getter=retention_confirm_getter,
        ),
    )
