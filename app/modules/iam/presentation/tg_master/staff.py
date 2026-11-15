"""Staff dialog: list, add by telegram_user_id (Q3), card (read-only roles), status, shops.

Reading the section needs ``moderators.manage`` or ``roles.manage`` (global): checked in
every getter and handler, because hidden buttons and forged item ids still reach them.
Shops on a card are intersected with the viewer's scope. Mutations are authorized by the
admin services.
"""

from __future__ import annotations

import re
from operator import itemgetter
from typing import Any

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import Button, Cancel, Column, ScrollingGroup, Select, SwitchTo
from aiogram_dialog.widgets.text import Format

from app.core.i18n import gettext as _
from app.modules.iam.admin_service import (
    USER_DELETED,
    USER_EXISTS,
    AccessAdminService,
    TenantAdminService,
    UserAdminService,
)
from app.modules.iam.permissions import MODERATORS_MANAGE, ROLES_MANAGE, SETTINGS_MANAGE
from app.modules.iam.presentation.tg_master.common import (
    BIGINT_MAX,
    actor_id,
    can_view_staff,
    compose,
    denied_text,
    error_text,
    in_scope,
    load_tenant,
    mark,
    mutate,
    nav_texts,
    resolve,
    set_notice,
    short,
    snapshot_of,
    status_text,
)
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.iam.presentation.tg_master.states import StaffSG

__all__ = ["parse_telegram_id", "staff_dialog"]

PAGE_SIZE = 10
_ID_RE = re.compile(r"[0-9]{1,19}")


def parse_telegram_id(text: str) -> int:
    value = text.strip()
    if not _ID_RE.fullmatch(value):
        raise ValueError("telegram_user_id must be a number")
    number = int(value)
    if not 0 < number < BIGINT_MAX:
        raise ValueError("telegram_user_id out of range")
    return number


def _target(manager: DialogManager) -> int | None:
    try:
        return parse_telegram_id(str(manager.dialog_data.get("user_id", "")))
    except ValueError:
        return None


def _can_moderate(manager: DialogManager) -> bool:
    snapshot = snapshot_of(manager)
    if snapshot.decide(MODERATORS_MANAGE).allowed:
        return True
    return any(snapshot.decide(MODERATORS_MANAGE, t).allowed for t in snapshot.tenants)


def _user_label(user: Any) -> str:
    name = f"@{user.telegram_user_name}" if user.telegram_user_name else "—"
    suffix = " 🗑" if user.is_deleted else ("" if user.is_active else " ⏸")
    return short(f"{name} ({user.telegram_user_id})") + suffix


def _allowed(manager: DialogManager) -> bool:
    return can_view_staff(snapshot_of(manager))


async def _guard(event: CallbackQuery | Message, manager: DialogManager) -> bool:
    """Handler gate: without staff read access close the dialog with a notice."""
    if _allowed(manager):
        return True
    if isinstance(event, CallbackQuery):
        await event.answer(denied_text())
    await manager.done()
    return False


def _denied(manager: DialogManager, **extra: Any) -> dict[str, Any]:
    return {"text": compose(manager, denied_text()), **extra, **nav_texts()}


# --- getters ---------------------------------------------------------------------------


async def list_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _denied(dialog_manager, users=[], can_add=False, btn_add="")
    queries = await resolve(dialog_manager, IamAdminQueries)
    users = await queries.users()
    return {
        "text": compose(dialog_manager, _("Сотрудники: {count}").format(count=len(users))),
        "users": [(str(u.telegram_user_id), _user_label(u)) for u in users],
        "can_add": snapshot_of(dialog_manager).decide(MODERATORS_MANAGE).allowed,
        "btn_add": _("Добавить сотрудника"),
        **nav_texts(),
    }


async def card_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "is_active": False,
        "is_inactive": False,
        "is_live": False,
        "is_deleted": False,
        "can_moderate": False,
        "btn_tenants": _("Магазины сотрудника"),
    }
    if not _allowed(dialog_manager):
        return _denied(dialog_manager, **data)
    target = _target(dialog_manager)
    queries = await resolve(dialog_manager, IamAdminQueries)
    user = await queries.user(target) if target is not None else None
    if user is None:
        return {
            "text": compose(dialog_manager, error_text("user_not_found")),
            **data,
            **nav_texts(),
        }
    snapshot = snapshot_of(dialog_manager)
    roles = await queries.user_roles(user.id)
    # Tenant soft-delete keeps ``tenant_moderators`` rows: deleted shops are shown only to
    # ``settings.manage`` (the same rule as ``load_tenant``), live ones within the scope.
    see_deleted = snapshot.decide(SETTINGS_MANAGE).allowed
    shops = [
        t
        for t in await queries.user_tenants(user.id)
        if (see_deleted if t.is_deleted else in_scope(snapshot, t.id))
    ]
    lines = [
        _("Сотрудник: {label}").format(label=_user_label(user)),
        _("Статус: {status}").format(
            status=status_text(is_active=user.is_active, is_deleted=user.is_deleted)
        ),
    ]
    if user.is_superuser:
        lines.append(_("Суперпользователь"))
    lines.append(_("Роли (только просмотр):"))
    lines.extend([f"• {role}" for role in roles] or ["—"])
    lines.append(_("Магазины:"))
    lines.extend(
        [f"• {t.name} ({t.slug})" + (" 🗑" if t.is_deleted else "") for t in shops] or ["—"]
    )
    is_self = user.telegram_user_id == actor_id(dialog_manager)
    mutable = not is_self and not user.is_superuser
    status = mutable and snapshot.decide(ROLES_MANAGE).allowed
    data.update(
        text=compose(dialog_manager, *lines),
        is_active=status and user.is_active and not user.is_deleted,
        is_inactive=status and not user.is_active and not user.is_deleted,
        is_live=status and not user.is_deleted,
        is_deleted=status and user.is_deleted,
        can_moderate=mutable and _can_moderate(dialog_manager) and not user.is_deleted,
    )
    return {**data, **nav_texts()}


async def tenants_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager) or not _can_moderate(dialog_manager):
        return _denied(dialog_manager, tenants=[])
    target = _target(dialog_manager)
    queries = await resolve(dialog_manager, IamAdminQueries)
    user = await queries.user(target) if target is not None else None
    if user is None:
        return {
            "text": compose(dialog_manager, error_text("user_not_found")),
            "tenants": [],
            **nav_texts(),
        }
    assigned = {t.id for t in await queries.user_tenants(user.id)}
    service = await resolve(dialog_manager, TenantAdminService)
    tenants = await service.list_tenants(actor_id(dialog_manager))
    return {
        "text": compose(dialog_manager, _("Нажмите на магазин, чтобы привязать или отвязать")),
        "tenants": [
            (str(t.id), short(f"{mark(t.id in assigned)} {t.name} ({t.slug})")) for t in tenants
        ],
        **nav_texts(),
    }


async def prompt_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _denied(dialog_manager)
    prompt = _(
        "Введите Telegram ID сотрудника (число). Username подхватится при его первом /start."
    )
    return {"text": compose(dialog_manager, prompt), **nav_texts()}


async def confirm_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    questions = {
        "delete": _("Удалить сотрудника? Его роли и привязки к магазинам будут сняты."),
        "deactivate": _("Деактивировать сотрудника?"),
    }
    action = str(dialog_manager.dialog_data.get("confirm", ""))
    return {"text": compose(dialog_manager, questions.get(action, "")), **nav_texts()}


# --- handlers --------------------------------------------------------------------------


async def on_user(callback: CallbackQuery, widget: Any, manager: DialogManager, item: str) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data["user_id"] = item
    await manager.switch_to(StaffSG.card)


async def on_invalid(message: Message, widget: Any, manager: DialogManager, error: Any) -> None:
    set_notice(manager, _("Нужен положительный числовой Telegram ID (до 19 цифр)."))


async def on_add(message: Message, widget: Any, manager: DialogManager, value: int) -> None:
    if not await _guard(message, manager):
        return
    actor = actor_id(manager)
    outcome = await mutate(manager, UserAdminService, lambda s: s.add_by_telegram_id(actor, value))
    if outcome.ok or outcome.code in {USER_EXISTS, USER_DELETED}:
        # Existing / deleted user: open the card (status, "Restore") with the notice.
        manager.dialog_data["user_id"] = str(value)
        await manager.switch_to(StaffSG.card)


async def _target_or_back(event: CallbackQuery, manager: DialogManager) -> int | None:
    if not await _guard(event, manager):
        return None
    target = _target(manager)
    if target is None:
        set_notice(manager, error_text("user_not_found"))
        await manager.switch_to(StaffSG.list)
    return target


async def on_activate(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    target = await _target_or_back(callback, manager)
    if target is not None:
        actor = actor_id(manager)
        await mutate(manager, UserAdminService, lambda s: s.activate(actor, target))


async def on_restore(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    target = await _target_or_back(callback, manager)
    if target is not None:
        actor = actor_id(manager)
        await mutate(manager, UserAdminService, lambda s: s.restore(actor, target))


async def on_ask(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    manager.dialog_data["confirm"] = button.widget_id
    await manager.switch_to(StaffSG.confirm)


async def on_confirm(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    action = manager.dialog_data.pop("confirm", "")
    target = await _target_or_back(callback, manager)
    if target is None:
        return
    actor = actor_id(manager)
    if action == "delete":
        await mutate(manager, UserAdminService, lambda s: s.soft_delete(actor, target))
    elif action == "deactivate":
        await mutate(manager, UserAdminService, lambda s: s.deactivate(actor, target))
    await manager.switch_to(StaffSG.card)


async def on_tenant(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item: str
) -> None:
    target = await _target_or_back(callback, manager)
    if target is None:
        return
    if not _can_moderate(manager):
        set_notice(manager, denied_text())
        return
    found = await load_tenant(manager, item)
    queries = await resolve(manager, IamAdminQueries)
    user = await queries.user(target)
    if found is None or found[0].is_deleted or user is None:
        set_notice(manager, error_text("tenant_not_found" if user else "user_not_found"))
        return
    actor, tenant_id = actor_id(manager), found[0].id
    if tenant_id in {t.id for t in await queries.user_tenants(user.id)}:
        await mutate(
            manager, AccessAdminService, lambda s: s.unassign_moderator(actor, target, tenant_id)
        )
    else:
        await mutate(
            manager, AccessAdminService, lambda s: s.assign_moderator(actor, target, tenant_id)
        )


# --- dialog ----------------------------------------------------------------------------


def _back(state: Any, widget_id: str) -> SwitchTo:
    return SwitchTo(Format("{btn_back}"), id=widget_id, state=state)


def _select(widget_id: str, items: str, on_click: Any) -> Select[str]:
    return Select(
        Format("{item[1]}"),
        id=widget_id,
        item_id_getter=itemgetter(0),
        items=items,
        on_click=on_click,
    )


def staff_dialog() -> Dialog:
    """New :class:`Dialog` instance (a dialog router can be included only once)."""
    text = Format("{text}")
    return Dialog(
        Window(
            text,
            ScrollingGroup(
                _select("u_sel", "users", on_user),
                id="u_scroll",
                width=1,
                height=PAGE_SIZE,
                hide_on_single_page=True,
            ),
            SwitchTo(Format("{btn_add}"), id="u_add", state=StaffSG.add, when="can_add"),
            Cancel(Format("{btn_back}")),
            state=StaffSG.list,
            getter=list_getter,
        ),
        Window(
            text,
            TextInput(
                id="u_tg_id", type_factory=parse_telegram_id, on_success=on_add, on_error=on_invalid
            ),
            _back(StaffSG.list, "u_add_back"),
            state=StaffSG.add,
            getter=prompt_getter,
        ),
        Window(
            text,
            Column(
                SwitchTo(
                    Format("{btn_tenants}"),
                    id="u_tenants",
                    state=StaffSG.tenants,
                    when="can_moderate",
                ),
                Button(
                    Format("{btn_deactivate}"), id="deactivate", on_click=on_ask, when="is_active"
                ),
                Button(
                    Format("{btn_activate}"),
                    id="u_activate",
                    on_click=on_activate,
                    when="is_inactive",
                ),
                Button(Format("{btn_delete}"), id="delete", on_click=on_ask, when="is_live"),
                Button(
                    Format("{btn_restore}"), id="u_restore", on_click=on_restore, when="is_deleted"
                ),
                _back(StaffSG.list, "u_card_back"),
            ),
            state=StaffSG.card,
            getter=card_getter,
        ),
        Window(
            text,
            ScrollingGroup(
                _select("u_t_sel", "tenants", on_tenant),
                id="u_t_scroll",
                width=1,
                height=PAGE_SIZE,
                hide_on_single_page=True,
            ),
            _back(StaffSG.card, "u_t_back"),
            state=StaffSG.tenants,
            getter=tenants_getter,
        ),
        Window(
            text,
            Button(Format("{btn_yes}"), id="u_yes", on_click=on_confirm),
            _back(StaffSG.card, "u_no"),
            state=StaffSG.confirm,
            getter=confirm_getter,
        ),
    )
