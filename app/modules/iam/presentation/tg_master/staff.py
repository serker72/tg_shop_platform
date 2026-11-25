"""Staff dialog: list, add by telegram_user_id (Q3), card, status, shops, roles, overrides.

Reading the section needs the global ``users.view``: checked in every getter and handler,
because hidden buttons and forged item ids still reach them. "Add" and status buttons are
shown by ``users.manage``, shop assignment by ``moderators.manage``. Shops on a card are
intersected with the viewer's scope. Mutations are authorized by the admin services.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from operator import itemgetter
from typing import Any
from uuid import UUID

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, StartMode, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import Button, Cancel, Column, ScrollingGroup, Select, SwitchTo
from aiogram_dialog.widgets.text import Format

from app.core.config import Settings
from app.core.i18n import gettext as _
from app.modules.iam.admin_service import (
    USER_DELETED,
    USER_EXISTS,
    AccessAdminService,
    TenantAdminService,
    UserAdminService,
)
from app.modules.iam.permissions import (
    MODERATORS_MANAGE,
    ROLES_MANAGE,
    TENANTS_MANAGE,
    USERS_MANAGE,
)
from app.modules.iam.presentation.tg_master.common import (
    BIGINT_MAX,
    can_view_staff,
    in_scope,
    load_tenant,
    mark,
    parse_uuid,
    snapshot_of,
    status_text,
)
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.iam.presentation.tg_master.states import EffectiveSG, OverridesSG, StaffSG
from app.shared_kernel.presentation.tg_master.common import (
    actor_id,
    add_notice,
    compose,
    denied_text,
    error_text,
    mutate,
    nav_texts,
    resolve,
    set_notice,
    short,
)
from app.shared_kernel.timezones import (
    TIMEZONE_PRESETS,
    effective_staff_timezone,
    search_timezones,
    suggest_timezones_by_time,
    validate_timezone,
)

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


async def _has_global_access(manager: DialogManager, user: Any) -> bool:
    """Superuser / global ``tenants.view_all``: never assigned to shops (the service checks)."""
    queries = await resolve(manager, IamAdminQueries)
    return await queries.has_global_access(user)


def _can_roles(manager: DialogManager) -> bool:
    return snapshot_of(manager).decide(ROLES_MANAGE).allowed


async def _roles_user(manager: DialogManager) -> Any:
    """Target of role / override management, the server side of ``can_roles``: staff read
    access and ``roles.manage``, not the actor, not a superuser, not soft-deleted."""
    if not _allowed(manager) or not _can_roles(manager):
        return None
    target = _target(manager)
    if target is None or target == actor_id(manager):
        return None
    user = await (await resolve(manager, IamAdminQueries)).user(target)
    if user is None or user.is_superuser or user.is_deleted:
        return None
    return user


def _user_label(user: Any) -> str:
    name = f"@{user.telegram_user_name}" if user.telegram_user_name else "—"
    suffix = " 🗑" if user.is_deleted else ("" if user.is_active else " ⏸")
    return short(f"{name} ({user.telegram_user_id})") + suffix


async def _default_timezone(manager: DialogManager) -> str:
    try:
        return (await resolve(manager, Settings)).default_timezone
    except KeyError:
        return Settings().default_timezone


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
        "can_add": snapshot_of(dialog_manager).decide(USERS_MANAGE).allowed,
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
        "can_roles": False,
        "has_user": False,
        "btn_effective": _("Фактические права"),
        "btn_tenants": _("Магазины сотрудника"),
        "btn_roles": _("Роли сотрудника"),
        "btn_overrides": _("Персональные права"),
        "btn_timezone": _("Мой часовой пояс"),
        "can_timezone": False,
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
    # ``tenants.manage`` (the same rule as ``load_tenant``), live ones within the scope.
    see_deleted = snapshot.decide(TENANTS_MANAGE).allowed
    shops = [
        t
        for t in await queries.user_tenants(user.id)
        if (see_deleted if t.is_deleted else in_scope(snapshot, t.id))
    ]
    lines = [
        _("Сотрудник: {label}").format(label=_user_label(user)),
        _("Часовой пояс: {timezone}").format(
            timezone=effective_staff_timezone(
                getattr(user, "timezone", None), await _default_timezone(dialog_manager)
            )
        ),
        _("Статус: {status}").format(
            status=status_text(is_active=user.is_active, is_deleted=user.is_deleted)
        ),
    ]
    if user.is_superuser:
        lines.append(_("Суперпользователь"))
    lines.append(_("Роли:"))
    lines.extend([f"• {role}" for role in roles] or ["—"])
    lines.append(_("Магазины:"))
    lines.extend(
        [f"• {t.name} ({t.slug})" + (" 🗑" if t.is_deleted else "") for t in shops] or ["—"]
    )
    is_self = user.telegram_user_id == actor_id(dialog_manager)
    mutable = not is_self and not user.is_superuser
    status = mutable and snapshot.decide(USERS_MANAGE).allowed
    data.update(
        text=compose(dialog_manager, *lines),
        is_active=status and user.is_active and not user.is_deleted,
        is_inactive=status and not user.is_active and not user.is_deleted,
        is_live=status and not user.is_deleted,
        is_deleted=status and user.is_deleted,
        can_roles=mutable and _can_roles(dialog_manager) and not user.is_deleted,
        can_timezone=user.telegram_user_id == actor_id(dialog_manager) and not user.is_deleted,
        has_user=True,
        can_moderate=mutable
        and _can_moderate(dialog_manager)
        and not user.is_deleted
        and not await _has_global_access(dialog_manager, user),
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
    if await _has_global_access(dialog_manager, user):
        # Forged callback: the card hides the button, the service refuses anyway.
        return {
            "text": compose(dialog_manager, error_text("moderator_has_global_access")),
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


async def roles_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    user = await _roles_user(dialog_manager)
    if user is None:
        return _denied(dialog_manager, roles=[])
    queries = await resolve(dialog_manager, IamAdminQueries)
    assigned = await queries.user_role_ids(user.id)
    roles = [r for r in await queries.roles() if r.is_active or r.id in assigned]
    prompt = _("Роли сотрудника {id}. Нажмите на роль, чтобы назначить или снять её.")
    return {
        "text": compose(dialog_manager, prompt.format(id=user.telegram_user_id)),
        "roles": [
            (str(r.id), short(f"{mark(r.id in assigned)} {r.name} ({r.slug})")) for r in roles
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
        "revoke_role": _("Снять роль «{name}» с сотрудника?").format(
            name=dialog_manager.dialog_data.get("role_name", "")
        ),
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
    if action == "revoke_role":
        manager.dialog_data.pop("role_name", None)
        await _revoke_role(manager, target, parse_uuid(manager.dialog_data.pop("role_id", None)))
        await manager.switch_to(StaffSG.roles)
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
    assigned = tenant_id in {t.id for t in await queries.user_tenants(user.id)}
    if not assigned and await _has_global_access(manager, user):
        set_notice(manager, error_text("moderator_has_global_access"))
        return
    if tenant_id in {t.id for t in await queries.user_tenants(user.id)}:
        await mutate(
            manager, AccessAdminService, lambda s: s.unassign_moderator(actor, target, tenant_id)
        )
    else:
        await mutate(
            manager, AccessAdminService, lambda s: s.assign_moderator(actor, target, tenant_id)
        )


async def on_role(callback: CallbackQuery, widget: Any, manager: DialogManager, item: str) -> None:
    """Assign at once; revoking an assigned role asks for confirmation."""
    target = await _target_or_back(callback, manager)
    if target is None:
        return
    user = await _roles_user(manager)
    if user is None:
        set_notice(manager, denied_text())
        return
    role_id = parse_uuid(item)
    queries = await resolve(manager, IamAdminQueries)
    role = await queries.role(role_id) if role_id is not None else None
    if role is None:
        set_notice(manager, error_text("role_not_found"))
        return
    if role.id in await queries.user_role_ids(user.id):
        manager.dialog_data.update(confirm="revoke_role", role_id=str(role.id), role_name=role.name)
        await manager.switch_to(StaffSG.confirm)
        return
    actor, picked = actor_id(manager), role.id
    # The service drops shop assignments when the role brings tenants.view_all.
    before = {t.id for t in await queries.user_tenants(user.id)}
    outcome = await mutate(
        manager, AccessAdminService, lambda s: s.assign_role(actor, target, picked)
    )
    after = {t.id for t in await queries.user_tenants(user.id)}
    if outcome.ok and before - after:
        add_notice(manager, _("Привязки к магазинам сняты: у роли доступ ко всем магазинам."))


async def _revoke_role(manager: DialogManager, target: int, role_id: UUID | None) -> None:
    if role_id is None or await _roles_user(manager) is None:
        set_notice(manager, denied_text())
        return
    actor, picked = actor_id(manager), role_id
    await mutate(manager, AccessAdminService, lambda s: s.revoke_role(actor, target, picked))


def _parse_onboarding_query(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        return value
    if not value:
        raise ValueError("empty timezone query")
    return value


async def self_timezone_user(manager: DialogManager) -> Any:
    from app.entrypoints.master_bot.middlewares import has_panel_access

    if not has_panel_access(snapshot_of(manager)):
        return None
    user = await (await resolve(manager, IamAdminQueries)).user(actor_id(manager))
    return user if user is not None and user.is_active and not user.is_deleted else None


async def on_onboarding_skip(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    from app.entrypoints.master_bot.menu import MainMenu

    if await self_timezone_user(manager) is None:
        await callback.answer(denied_text())
        return
    await manager.start(MainMenu.main, mode=StartMode.RESET_STACK)


async def on_onboarding_query(
    message: Message, widget: Any, manager: DialogManager, value: str
) -> None:
    if await self_timezone_user(manager) is None:
        return
    query = _parse_onboarding_query(value)
    if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", query):
        zones = suggest_timezones_by_time(query, datetime.now(UTC))
    else:
        zones = [zone for zone, _ in search_timezones(query)]
    manager.dialog_data["onboarding_zones"] = zones[:10]
    await manager.switch_to(StaffSG.onboarding_results)


async def onboarding_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    dialog_manager.dialog_data.pop("onboarding_zones", None)
    if await self_timezone_user(dialog_manager) is None:
        return _denied(dialog_manager, zones=[], btn_skip=_("Пропустить"))
    return {
        "text": compose(
            dialog_manager,
            _("Настройте часовой пояс. Выберите зону или введите HH:MM / город."),
            _("Часовой пояс: {timezone}").format(timezone=await _default_timezone(dialog_manager)),
        ),
        "zones": [(zone, zone) for zone in TIMEZONE_PRESETS],
        "btn_skip": _("Пропустить"),
    }


async def onboarding_results_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    zones = dialog_manager.dialog_data.get("onboarding_zones", [])
    if await self_timezone_user(dialog_manager) is None:
        return _denied(dialog_manager, zones=[], btn_skip=_("Пропустить"))
    return {
        "text": compose(dialog_manager, _("Выберите часовой пояс")),
        "zones": [(z, z) for z in zones],
        "btn_skip": _("Пропустить"),
        **nav_texts(),
    }


async def on_onboarding_zone(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item: str
) -> None:
    state = manager.current_context().state
    zones: tuple[str, ...] = ()
    if state == StaffSG.onboarding:
        zones = TIMEZONE_PRESETS
    elif state == StaffSG.onboarding_results:
        zones = manager.dialog_data.get("onboarding_zones", ())
    if item not in zones or await self_timezone_user(manager) is None:
        await callback.answer(denied_text())
        return
    try:
        validate_timezone(item)
    except ValueError:
        set_notice(manager, error_text("invalid_timezone"))
        return
    from app.entrypoints.master_bot.menu import MainMenu

    outcome = await mutate(
        manager, UserAdminService, lambda s: s.set_timezone(actor_id(manager), item)
    )
    if outcome.ok:
        await manager.start(MainMenu.main, mode=StartMode.RESET_STACK)


async def open_self_timezone(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    if await self_timezone_user(manager) is None:
        await callback.answer(denied_text())
        return
    await manager.start(StaffSG.timezone_presets)


async def on_timezone(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if await _target_or_back(callback, manager) != actor_id(manager):
        await callback.answer(denied_text())
        return
    await open_self_timezone(callback, button, manager)


async def _timezone_getter(manager: DialogManager, zones: Any) -> dict[str, Any]:
    user = await self_timezone_user(manager)
    if user is None:
        return _denied(manager, zones=[])
    effective = effective_staff_timezone(
        getattr(user, "timezone", None), await _default_timezone(manager)
    )
    return {
        "text": compose(
            manager,
            _("Часовой пояс: {timezone}").format(timezone=effective),
            _("Введите город, IANA zone или смещение (например +2)"),
        ),
        "zones": [(z, z) for z in zones],
        **nav_texts(),
    }


async def timezone_presets_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    dialog_manager.dialog_data.pop("self_timezone_zones", None)
    return await _timezone_getter(dialog_manager, TIMEZONE_PRESETS)


async def timezone_results_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    return await _timezone_getter(
        dialog_manager, dialog_manager.dialog_data.get("self_timezone_zones", [])
    )


async def on_timezone_query(
    message: Message, widget: Any, manager: DialogManager, value: str
) -> None:
    if await self_timezone_user(manager) is None:
        return
    manager.dialog_data["self_timezone_zones"] = [z for z, _ in search_timezones(value)[:10]]
    await manager.switch_to(StaffSG.timezone_results)


async def on_timezone_value(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item: str
) -> None:
    state = manager.current_context().state
    zones: tuple[str, ...] = ()
    if state == StaffSG.timezone_presets:
        zones = TIMEZONE_PRESETS
    elif state == StaffSG.timezone_results:
        zones = manager.dialog_data.get("self_timezone_zones", ())
    if item not in zones or await self_timezone_user(manager) is None:
        await callback.answer(denied_text())
        return
    try:
        value = validate_timezone(item)
    except ValueError:
        set_notice(manager, error_text("invalid_timezone"))
        return
    outcome = await mutate(
        manager, UserAdminService, lambda s: s.set_timezone(actor_id(manager), value)
    )
    if outcome.ok:
        await manager.done()


async def on_effective(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    """Effective permissions: reading needs ``users.view`` (checked by the guard)."""
    target = await _target_or_back(callback, manager)
    if target is not None:
        await manager.start(EffectiveSG.list, data={"user_id": str(target)})


async def on_overrides(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    target = await _target_or_back(callback, manager)
    if target is None:
        return
    if await _roles_user(manager) is None:
        await callback.answer(denied_text())
        return
    await manager.start(OverridesSG.list, data={"user_id": str(target)})


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
                _select("u_onboarding_preset", "zones", on_onboarding_zone),
                id="u_onboarding_preset_scroll",
                width=1,
                height=8,
            ),
            TextInput(
                id="u_onboarding_query",
                type_factory=_parse_onboarding_query,
                on_success=on_onboarding_query,
                on_error=on_invalid,
            ),
            Button(Format("{btn_skip}"), id="u_onboarding_skip", on_click=on_onboarding_skip),
            state=StaffSG.onboarding,
            getter=onboarding_getter,
        ),
        Window(
            text,
            ScrollingGroup(
                _select("u_onboarding_zone", "zones", on_onboarding_zone),
                id="u_onboarding_scroll",
                width=1,
                height=10,
            ),
            Button(
                Format("{btn_skip}"), id="u_onboarding_skip_results", on_click=on_onboarding_skip
            ),
            _back(StaffSG.onboarding, "u_onboarding_back"),
            state=StaffSG.onboarding_results,
            getter=onboarding_results_getter,
        ),
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
                    Format("{btn_effective}"), id="u_eff", on_click=on_effective, when="has_user"
                ),
                SwitchTo(
                    Format("{btn_roles}"), id="u_roles", state=StaffSG.roles, when="can_roles"
                ),
                Button(
                    Format("{btn_overrides}"), id="u_ovr", on_click=on_overrides, when="can_roles"
                ),
                Button(
                    Format("{btn_timezone}"),
                    id="u_timezone",
                    on_click=on_timezone,
                    when="can_timezone",
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
            ScrollingGroup(
                _select("u_r_sel", "roles", on_role),
                id="u_r_scroll",
                width=1,
                height=PAGE_SIZE,
                hide_on_single_page=True,
            ),
            _back(StaffSG.card, "u_r_back"),
            state=StaffSG.roles,
            getter=roles_getter,
        ),
        Window(
            text,
            Button(Format("{btn_yes}"), id="u_yes", on_click=on_confirm),
            _back(StaffSG.card, "u_no"),
            state=StaffSG.confirm,
            getter=confirm_getter,
        ),
        Window(
            text,
            ScrollingGroup(
                _select("u_tz", "zones", on_timezone_value), id="u_tz_scroll", width=1, height=10
            ),
            TextInput(id="u_tz_query", on_success=on_timezone_query),
            Cancel(Format("{btn_back}"), id="u_tz_back"),
            state=StaffSG.timezone_presets,
            getter=timezone_presets_getter,
        ),
        Window(
            text,
            Column(_select("u_tz_result", "zones", on_timezone_value)),
            TextInput(id="u_tz_retry", on_success=on_timezone_query),
            _back(StaffSG.timezone_presets, "u_tz_results_back"),
            state=StaffSG.timezone_results,
            getter=timezone_results_getter,
        ),
    )
