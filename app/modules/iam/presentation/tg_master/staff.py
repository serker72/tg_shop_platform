"""Staff dialog: list, add by telegram_user_id (Q3), card (read-only roles), status, shops."""

from __future__ import annotations

from operator import itemgetter
from typing import Any
from uuid import UUID

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import Button, Cancel, Column, ScrollingGroup, Select, SwitchTo
from aiogram_dialog.widgets.text import Format

from app.core.i18n import gettext as _
from app.modules.iam.admin_service import AccessAdminService, TenantAdminService, UserAdminService
from app.modules.iam.permissions import MODERATORS_MANAGE, ROLES_MANAGE
from app.modules.iam.presentation.tg_master.common import (
    actor_id,
    compose,
    error_text,
    mark,
    mutate,
    nav_texts,
    resolve,
    set_notice,
    snapshot_of,
    status_text,
)
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.iam.presentation.tg_master.states import StaffSG

__all__ = ["parse_telegram_id", "staff_dialog"]

PAGE_SIZE = 10


def parse_telegram_id(text: str) -> int:
    value = int(text.strip())
    if value <= 0:
        raise ValueError("telegram_user_id must be positive")
    return value


def _target(manager: DialogManager) -> int:
    return int(manager.dialog_data["user_id"])


def _can_moderate(manager: DialogManager) -> bool:
    snapshot = snapshot_of(manager)
    if snapshot.decide(MODERATORS_MANAGE).allowed:
        return True
    return any(snapshot.decide(MODERATORS_MANAGE, t).allowed for t in snapshot.tenants)


def _user_label(user: Any) -> str:
    name = f"@{user.telegram_user_name}" if user.telegram_user_name else "—"
    suffix = "" if user.is_active and not user.is_deleted else " ⏸"
    return f"{name} ({user.telegram_user_id}){suffix}"


# --- getters ---------------------------------------------------------------------------


async def list_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
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
    queries = await resolve(dialog_manager, IamAdminQueries)
    user = await queries.user(_target(dialog_manager))
    data: dict[str, Any] = {
        "is_active": False,
        "is_inactive": False,
        "is_live": False,
        "is_deleted": False,
        "can_moderate": False,
        "btn_tenants": _("Магазины сотрудника"),
        **nav_texts(),
    }
    if user is None:
        data["text"] = compose(dialog_manager, error_text("user_not_found"))
        return data
    roles = await queries.user_roles(user.id)
    shops = await queries.user_tenants(user.id)
    role_lines = [f"• {role}" for role in roles]
    lines = [
        _("Сотрудник: {label}").format(label=_user_label(user)),
        _("Статус: {status}").format(
            status=status_text(is_active=user.is_active, is_deleted=user.is_deleted)
        ),
    ]
    if user.is_superuser:
        lines.append(_("Суперпользователь"))
    lines.append(_("Роли (только просмотр):"))
    lines.extend(role_lines or ["—"])
    lines.append(_("Магазины:"))
    lines.extend([f"• {t.name} ({t.slug})" for t in shops] or ["—"])
    status = snapshot_of(dialog_manager).decide(ROLES_MANAGE).allowed and not user.is_superuser
    data.update(
        text=compose(dialog_manager, *lines),
        is_active=status and user.is_active and not user.is_deleted,
        is_inactive=status and not user.is_active and not user.is_deleted,
        is_live=status and not user.is_deleted,
        is_deleted=status and user.is_deleted,
        can_moderate=_can_moderate(dialog_manager) and not user.is_deleted,
    )
    return data


async def tenants_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    queries = await resolve(dialog_manager, IamAdminQueries)
    service = await resolve(dialog_manager, TenantAdminService)
    user = await queries.user(_target(dialog_manager))
    assigned = {t.id for t in await queries.user_tenants(user.id)} if user else set()
    tenants = await service.list_tenants(actor_id(dialog_manager))
    return {
        "text": compose(dialog_manager, _("Нажмите на магазин, чтобы привязать или отвязать")),
        "tenants": [(str(t.id), f"{mark(t.id in assigned)} {t.name} ({t.slug})") for t in tenants],
        **nav_texts(),
    }


async def prompt_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
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
    manager.dialog_data["user_id"] = item
    await manager.switch_to(StaffSG.card)


async def on_invalid(message: Message, widget: Any, manager: DialogManager, error: Any) -> None:
    set_notice(manager, _("Нужен положительный числовой Telegram ID."))


async def on_add(message: Message, widget: Any, manager: DialogManager, value: int) -> None:
    actor = actor_id(manager)
    ok, _user = await mutate(
        manager, UserAdminService, lambda s: s.add_by_telegram_id(actor, value)
    )
    if ok:
        manager.dialog_data["user_id"] = str(value)
        await manager.switch_to(StaffSG.card)


async def on_activate(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    actor, target = actor_id(manager), _target(manager)
    await mutate(manager, UserAdminService, lambda s: s.activate(actor, target))


async def on_restore(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    actor, target = actor_id(manager), _target(manager)
    await mutate(manager, UserAdminService, lambda s: s.restore(actor, target))


async def on_ask(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    manager.dialog_data["confirm"] = button.widget_id
    await manager.switch_to(StaffSG.confirm)


async def on_confirm(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    action = manager.dialog_data.pop("confirm", "")
    actor, target = actor_id(manager), _target(manager)
    if action == "delete":
        await mutate(manager, UserAdminService, lambda s: s.soft_delete(actor, target))
    elif action == "deactivate":
        await mutate(manager, UserAdminService, lambda s: s.deactivate(actor, target))
    await manager.switch_to(StaffSG.card)


async def on_tenant(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item: str
) -> None:
    actor, target, tenant_id = actor_id(manager), _target(manager), UUID(item)
    queries = await resolve(manager, IamAdminQueries)
    user = await queries.user(target)
    if user is None:
        set_notice(manager, error_text("user_not_found"))
        return
    assigned = tenant_id in {t.id for t in await queries.user_tenants(user.id)}
    if assigned:
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


def staff_dialog() -> Dialog:
    """New :class:`Dialog` instance (a dialog router can be included only once)."""
    text = Format("{text}")
    return Dialog(
        Window(
            text,
            ScrollingGroup(
                Select(
                    Format("{item[1]}"),
                    id="u_sel",
                    item_id_getter=itemgetter(0),
                    items="users",
                    on_click=on_user,
                ),
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
                Select(
                    Format("{item[1]}"),
                    id="u_t_sel",
                    item_id_getter=itemgetter(0),
                    items="tenants",
                    on_click=on_tenant,
                ),
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
