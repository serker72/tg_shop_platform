"""Shops dialog: scoped list, card, create, rename, (de)activate, soft-delete/restore, settings.

Read access is re-checked in every getter/handler (:func:`load_tenant`): a forged
``callback_data`` with a foreign ``tenant_id`` or a hidden button gets "not found"/"no
access" and no data. Mutations are authorized by :class:`TenantAdminService`.
"""

from __future__ import annotations

import re
from operator import itemgetter
from typing import Any, Final

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import Button, Cancel, Column, ScrollingGroup, Select, SwitchTo
from aiogram_dialog.widgets.text import Format

from app.core.i18n import gettext as _
from app.modules.bot_manager.presentation.tg_master.states import BotsSG
from app.modules.iam.admin_service import TENANT_SETTINGS_FIELDS, TenantAdminService
from app.modules.iam.models import TENANT_SLUG_MAX_LENGTH, TENANT_SLUG_PATTERN
from app.modules.iam.permissions import TENANTS_MANAGE
from app.modules.iam.presentation.tg_master.common import (
    BIGINT_MAX,
    load_tenant,
    mark,
    snapshot_of,
    status_text,
    yes_no,
)
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.iam.presentation.tg_master.states import TenantsSG
from app.shared_kernel.presentation.tg_master.common import (
    actor_id,
    compose,
    denied_text,
    error_text,
    mutate,
    nav_texts,
    resolve,
    set_notice,
    short,
)
from app.shared_kernel.timezones import TIMEZONE_PRESETS, timezone_page, validate_timezone

__all__ = ["NAME_MAX_LENGTH", "parse_chat_id", "parse_name", "parse_slug", "tenants_dialog"]

PAGE_SIZE = 10
NAME_MAX_LENGTH: Final = 128
_LANGUAGE_FIELD = "default_language_code"
_CLEAR = "-"
_CHAT_RE = re.compile(r"-?[0-9]{1,19}")


def parse_name(text: str) -> str:
    name = text.strip()
    if not 1 <= len(name) <= NAME_MAX_LENGTH:
        raise ValueError("invalid name")
    return name


def parse_slug(text: str) -> str:
    slug = text.strip()
    if len(slug) > TENANT_SLUG_MAX_LENGTH or not re.fullmatch(TENANT_SLUG_PATTERN, slug):
        raise ValueError("invalid slug")
    return slug


def parse_chat_id(text: str) -> int | None:
    """Chat id (``-100…`` allowed), ``-`` clears; 0 and values outside bigint are rejected."""
    value = text.strip()
    if value == _CLEAR:
        return None
    if not _CHAT_RE.fullmatch(value):
        raise ValueError("invalid chat id")
    number = int(value)
    if number == 0 or not -BIGINT_MAX < number < BIGINT_MAX:
        raise ValueError("chat id out of range")
    return number


def _setting_label(key: str) -> str:
    labels = {
        "is_store_open": _("Магазин открыт"),
        "is_buy_enabled": _("Покупки включены"),
        "is_refill_enabled": _("Пополнение баланса"),
        "is_referral_enabled": _("Реферальная программа"),
        "is_notifications_enabled": _("Уведомления"),
        "default_language_code": _("Язык по умолчанию"),
        "support_chat_id": _("Чат поддержки"),
        "news_chat_id": _("Чат новостей"),
    }
    return labels.get(key, key)


def _fields(kind: str) -> list[str]:
    return [key for key, value in TENANT_SETTINGS_FIELDS.items() if value == kind]


def _can_manage(manager: DialogManager) -> bool:
    return snapshot_of(manager).decide(TENANTS_MANAGE).allowed


async def _current(manager: DialogManager) -> Any:
    return await load_tenant(manager, manager.dialog_data.get("tenant_id"))


def _not_found(manager: DialogManager, **extra: Any) -> dict[str, Any]:
    return {"text": compose(manager, error_text("tenant_not_found")), **extra, **nav_texts()}


# --- getters ---------------------------------------------------------------------------


async def list_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    service = await resolve(dialog_manager, TenantAdminService)
    tenants = await service.list_tenants(actor_id(dialog_manager))
    items = [
        (str(t.id), short(f"{t.name} ({t.slug})") + ("" if t.is_active else " ⏸")) for t in tenants
    ]
    title = (
        _("Магазины: {count}").format(count=len(items)) if items else _("Нет доступных магазинов")
    )
    return {
        "text": compose(dialog_manager, title),
        "tenants": items,
        "can_manage": _can_manage(dialog_manager),
        "btn_create": _("Создать магазин"),
        "btn_deleted": _("Удалённые магазины"),
        **nav_texts(),
    }


async def deleted_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _can_manage(dialog_manager):
        return {"text": compose(dialog_manager, denied_text()), "tenants": [], **nav_texts()}
    queries = await resolve(dialog_manager, IamAdminQueries)
    tenants = await queries.deleted_tenants()
    title = _("Удалённые магазины: {count}").format(count=len(tenants))
    return {
        "text": compose(dialog_manager, title),
        "tenants": [(str(t.id), short(f"{t.name} ({t.slug})")) for t in tenants],
        **nav_texts(),
    }


async def card_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "can_manage": False,
        "is_live": False,
        "is_active": False,
        "is_inactive": False,
        "is_deleted": False,
        "can_bots": False,
        "btn_bots": _("🤖 Боты"),
        "btn_rename": _("Переименовать"),
        "btn_settings": _("Настройки магазина"),
        "btn_timezone": _("Часовой пояс"),
    }
    found = await _current(dialog_manager)
    if found is None:
        return _not_found(dialog_manager, **data)
    tenant, settings = found
    lines = [
        _("Магазин: {name}").format(name=tenant.name),
        _("Slug: {slug}").format(slug=tenant.slug),
        _("Статус: {status}").format(
            status=status_text(is_active=tenant.is_active, is_deleted=tenant.is_deleted)
        ),
        _("Часовой пояс: {timezone}").format(timezone=getattr(tenant, "timezone", "UTC")),
    ]
    if settings is not None:
        for key in _fields("bool"):
            lines.append(f"{_setting_label(key)}: {yes_no(bool(getattr(settings, key)))}")
        language = getattr(settings, _LANGUAGE_FIELD, None)
        lines.append(f"{_setting_label(_LANGUAGE_FIELD)}: {language or _('глобальный')}")
        for key in _fields("chat"):
            lines.append(f"{_setting_label(key)}: {getattr(settings, key) or '—'}")
    manage = _can_manage(dialog_manager)
    can_bots = snapshot_of(dialog_manager).decide("bots.manage", tenant.id).allowed
    data.update(
        text=compose(dialog_manager, *lines),
        can_manage=manage,
        is_live=manage and not tenant.is_deleted,
        is_active=manage and tenant.is_active and not tenant.is_deleted,
        is_inactive=manage and not tenant.is_active and not tenant.is_deleted,
        is_deleted=manage and tenant.is_deleted,
        can_bots=can_bots,
    )
    return {**data, **nav_texts()}


async def settings_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    found = await _current(dialog_manager)
    if found is None or found[0].is_deleted:
        return _not_found(dialog_manager, flags=[], chats=[], btn_language="", btn_timezone="")
    settings = found[1]
    flags = [
        (key, f"{mark(bool(getattr(settings, key, False)))} {_setting_label(key)}")
        for key in _fields("bool")
    ]
    chats = [
        (key, f"{_setting_label(key)}: {getattr(settings, key, None) or '—'}")
        for key in _fields("chat")
    ]
    language = getattr(settings, _LANGUAGE_FIELD, None)
    return {
        "text": compose(dialog_manager, _("Настройки магазина")),
        "flags": flags,
        "chats": chats,
        "btn_language": f"{_setting_label(_LANGUAGE_FIELD)}: {language or _('глобальный')}",
        "btn_timezone": f"{_('Часовой пояс')}: {getattr(found[0], 'timezone', 'UTC')}",
        **nav_texts(),
    }


async def language_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    found = await _current(dialog_manager)
    if found is None or found[0].is_deleted:
        return _not_found(dialog_manager, languages=[])
    current = getattr(found[1], _LANGUAGE_FIELD, None)
    queries = await resolve(dialog_manager, IamAdminQueries)
    items = [(_CLEAR, f"{mark(current is None)} {_('Глобальный по умолчанию')}")]
    items += [
        (lang.code, short(f"{mark(current == lang.code)} {lang.name} ({lang.code})"))
        for lang in await queries.live_languages()
    ]
    return {
        "text": compose(dialog_manager, _("Выберите язык по умолчанию магазина")),
        "languages": items,
        **nav_texts(),
    }


async def prompt_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    state = dialog_manager.current_context().state
    prompts = {
        TenantsSG.create_name: _("Введите название магазина (до 128 символов)"),
        TenantsSG.create_slug: _(
            "Введите slug: латиница в нижнем регистре, цифры и дефисы, до 64 символов"
        ),
        TenantsSG.rename: _("Введите новое название магазина (до 128 символов)"),
        TenantsSG.chat_input: _(
            "Введите ID чата числом, например -1001234567890, или «-», чтобы очистить. "
            "Бот магазина должен быть участником этого чата."
        ),
    }
    return {"text": compose(dialog_manager, prompts.get(state, "")), **nav_texts()}


async def confirm_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    questions = {
        "delete": _("Удалить магазин? Его можно будет восстановить."),
        "deactivate": _("Деактивировать магазин?"),
    }
    action = str(dialog_manager.dialog_data.get("confirm", ""))
    return {"text": compose(dialog_manager, questions.get(action, "")), **nav_texts()}


# --- handlers --------------------------------------------------------------------------


async def on_tenant(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item: str
) -> None:
    if await load_tenant(manager, item) is None:
        set_notice(manager, error_text("tenant_not_found"))
        return
    manager.dialog_data["tenant_id"] = item
    await manager.switch_to(TenantsSG.card)


async def on_bots(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    found = await _current(manager)
    if found is None or not snapshot_of(manager).decide("bots.manage", found[0].id).allowed:
        await callback.answer(_("Нет доступа."))
        return
    await manager.start(BotsSG.list, data={"tenant_id": str(found[0].id)})


async def on_invalid(message: Message, widget: Any, manager: DialogManager, error: Any) -> None:
    set_notice(manager, _("Некорректное значение, попробуйте ещё раз."))


async def on_invalid_name(
    message: Message, widget: Any, manager: DialogManager, error: Any
) -> None:
    set_notice(manager, _("Название должно содержать от 1 до 128 символов."))


async def on_create(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not _can_manage(manager):
        set_notice(manager, denied_text())
        return
    manager.dialog_data.pop("new_name", None)
    await manager.switch_to(TenantsSG.create_name)


async def on_name(message: Message, widget: Any, manager: DialogManager, value: str) -> None:
    manager.dialog_data["new_name"] = value
    await manager.switch_to(TenantsSG.create_slug)


async def on_slug(message: Message, widget: Any, manager: DialogManager, value: str) -> None:
    actor, name = actor_id(manager), str(manager.dialog_data.get("new_name", ""))
    outcome = await mutate(
        manager, TenantAdminService, lambda s: s.create(actor, name=name, slug=value)
    )
    if outcome.ok and outcome.value is not None:
        manager.dialog_data["tenant_id"] = str(outcome.value.id)
        await manager.switch_to(TenantsSG.card)


async def _with_tenant(manager: DialogManager) -> Any:
    """``(actor, tenant)`` of the current card if still readable, else a notice + list."""
    found = await _current(manager)
    if found is None:
        set_notice(manager, error_text("tenant_not_found"))
        await manager.switch_to(TenantsSG.list)
        return None
    return found


async def on_rename(message: Message, widget: Any, manager: DialogManager, value: str) -> None:
    found = await _with_tenant(manager)
    if found is None:
        return
    actor, tenant_id = actor_id(manager), found[0].id
    outcome = await mutate(
        manager, TenantAdminService, lambda s: s.update(actor, tenant_id, name=value)
    )
    if outcome.ok:
        await manager.switch_to(TenantsSG.card)


async def on_activate(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    found = await _with_tenant(manager)
    if found is not None:
        actor, tenant_id = actor_id(manager), found[0].id
        await mutate(manager, TenantAdminService, lambda s: s.activate(actor, tenant_id))


async def on_restore(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    found = await _with_tenant(manager)
    if found is not None:
        actor, tenant_id = actor_id(manager), found[0].id
        await mutate(manager, TenantAdminService, lambda s: s.restore(actor, tenant_id))


async def on_ask(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    manager.dialog_data["confirm"] = button.widget_id
    await manager.switch_to(TenantsSG.confirm)


async def on_confirm(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    action = manager.dialog_data.pop("confirm", "")
    found = await _with_tenant(manager)
    if found is None:
        return
    actor, tenant_id = actor_id(manager), found[0].id
    if action == "delete":
        await mutate(manager, TenantAdminService, lambda s: s.soft_delete(actor, tenant_id))
    elif action == "deactivate":
        await mutate(manager, TenantAdminService, lambda s: s.deactivate(actor, tenant_id))
    await manager.switch_to(TenantsSG.card)


async def _update_settings(manager: DialogManager, changes: dict[str, Any]) -> bool:
    found = await _with_tenant(manager)
    if found is None:
        return False
    actor, tenant_id = actor_id(manager), found[0].id
    outcome = await mutate(
        manager, TenantAdminService, lambda s: s.update_settings(actor, tenant_id, changes)
    )
    return outcome.ok


async def on_flag(callback: CallbackQuery, widget: Any, manager: DialogManager, item: str) -> None:
    found = await _current(manager)
    if found is None or found[1] is None or item not in _fields("bool"):
        set_notice(manager, error_text("tenant_not_found"))
        return
    await _update_settings(manager, {item: not bool(getattr(found[1], item))})


async def on_timezone(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if await _current(manager) is None or not _can_manage(manager):
        await callback.answer(denied_text())
        return
    await manager.switch_to(TenantsSG.timezone_presets)


async def timezone_presets_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    found = await _current(dialog_manager)
    if found is None or found[0].is_deleted or not _can_manage(dialog_manager):
        return _not_found(dialog_manager, zones=[])
    current = getattr(found[0], "timezone", "UTC")
    return {
        "text": compose(dialog_manager, _("Выберите часовой пояс")),
        "zones": [(z, f"{'✅' if z == current else '▫️'} {z}") for z in TIMEZONE_PRESETS],
        "btn_search": _("Поиск"),
        **nav_texts(),
    }


async def on_timezone_preset(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item: str
) -> None:
    try:
        value = validate_timezone(item)
    except ValueError:
        set_notice(manager, error_text("invalid_timezone"))
        return
    found = await _current(manager)
    if found is None or not _can_manage(manager):
        set_notice(manager, denied_text())
        return
    outcome = await mutate(
        manager,
        TenantAdminService,
        lambda s: s.update(actor_id(manager), found[0].id, name=found[0].name, timezone=value),
    )
    if outcome.ok:
        await manager.switch_to(TenantsSG.settings)


async def on_timezone_search(
    message: Message, widget: Any, manager: DialogManager, value: str
) -> None:
    manager.dialog_data["timezone_query"] = value
    manager.dialog_data["timezone_page"] = 0
    await manager.switch_to(TenantsSG.timezone_results)


async def timezone_results_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    query = str(dialog_manager.dialog_data.get("timezone_query", ""))
    page = int(dialog_manager.dialog_data.get("timezone_page", 0))
    rows, has_next = timezone_page(query, page)
    return {
        "text": compose(dialog_manager, _("Результаты поиска: {query}").format(query=query)),
        "zones": [(z, f"{z} (UTC{offset})") for z, offset in rows],
        "has_next": has_next,
        "btn_next": _("Далее"),
        **nav_texts(),
    }


async def on_timezone_result(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item: str
) -> None:
    await on_timezone_preset(callback, widget, manager, item)


async def on_timezone_next(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    manager.dialog_data["timezone_page"] = int(manager.dialog_data.get("timezone_page", 0)) + 1
    await manager.switch_to(TenantsSG.timezone_results)


async def on_language(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item: str
) -> None:
    if await _update_settings(manager, {_LANGUAGE_FIELD: None if item == _CLEAR else item}):
        await manager.switch_to(TenantsSG.settings)


async def on_chat_field(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item: str
) -> None:
    if item not in _fields("chat"):
        return
    manager.dialog_data["chat_field"] = item
    await manager.switch_to(TenantsSG.chat_input)


async def on_chat(message: Message, widget: Any, manager: DialogManager, value: int | None) -> None:
    field = str(manager.dialog_data.get("chat_field", ""))
    if await _update_settings(manager, {field: value}):
        await manager.switch_to(TenantsSG.settings)


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


def tenants_dialog() -> Dialog:
    """New :class:`Dialog` instance (a dialog router can be included only once)."""
    text = Format("{text}")
    return Dialog(
        Window(
            text,
            ScrollingGroup(
                _select("t_sel", "tenants", on_tenant),
                id="t_scroll",
                width=1,
                height=PAGE_SIZE,
                hide_on_single_page=True,
            ),
            Button(Format("{btn_create}"), id="t_create", on_click=on_create, when="can_manage"),
            SwitchTo(
                Format("{btn_deleted}"), id="t_deleted", state=TenantsSG.deleted, when="can_manage"
            ),
            Cancel(Format("{btn_back}")),
            state=TenantsSG.list,
            getter=list_getter,
        ),
        Window(
            text,
            ScrollingGroup(
                _select("t_del_sel", "tenants", on_tenant),
                id="t_del_scroll",
                width=1,
                height=PAGE_SIZE,
                hide_on_single_page=True,
            ),
            _back(TenantsSG.list, "t_del_back"),
            state=TenantsSG.deleted,
            getter=deleted_getter,
        ),
        Window(
            text,
            Column(
                Button(Format("{btn_bots}"), id="t_bots", on_click=on_bots, when="can_bots"),
                SwitchTo(
                    Format("{btn_rename}"), id="t_rename", state=TenantsSG.rename, when="is_live"
                ),
                SwitchTo(
                    Format("{btn_settings}"),
                    id="t_settings",
                    state=TenantsSG.settings,
                    when="is_live",
                ),
                Button(
                    Format("{btn_deactivate}"), id="deactivate", on_click=on_ask, when="is_active"
                ),
                Button(
                    Format("{btn_activate}"),
                    id="t_activate",
                    on_click=on_activate,
                    when="is_inactive",
                ),
                Button(Format("{btn_delete}"), id="delete", on_click=on_ask, when="is_live"),
                Button(
                    Format("{btn_restore}"), id="t_restore", on_click=on_restore, when="is_deleted"
                ),
                _back(TenantsSG.list, "t_card_back"),
            ),
            state=TenantsSG.card,
            getter=card_getter,
        ),
        Window(
            text,
            TextInput(
                id="t_name", type_factory=parse_name, on_success=on_name, on_error=on_invalid_name
            ),
            _back(TenantsSG.list, "t_name_back"),
            state=TenantsSG.create_name,
            getter=prompt_getter,
        ),
        Window(
            text,
            TextInput(
                id="t_slug", type_factory=parse_slug, on_success=on_slug, on_error=on_invalid
            ),
            _back(TenantsSG.list, "t_slug_back"),
            state=TenantsSG.create_slug,
            getter=prompt_getter,
        ),
        Window(
            text,
            TextInput(
                id="t_new_name",
                type_factory=parse_name,
                on_success=on_rename,
                on_error=on_invalid_name,
            ),
            _back(TenantsSG.card, "t_rename_back"),
            state=TenantsSG.rename,
            getter=prompt_getter,
        ),
        Window(
            text,
            Column(
                _select("t_flag", "flags", on_flag),
                SwitchTo(Format("{btn_language}"), id="t_lang", state=TenantsSG.language),
                Button(Format("{btn_timezone}"), id="t_timezone", on_click=on_timezone),
                _select("t_chat", "chats", on_chat_field),
                _back(TenantsSG.card, "t_settings_back"),
            ),
            state=TenantsSG.settings,
            getter=settings_getter,
        ),
        Window(
            text,
            Column(
                _select("t_lang_sel", "languages", on_language),
                _back(TenantsSG.settings, "t_lang_back"),
            ),
            state=TenantsSG.language,
            getter=language_getter,
        ),
        Window(
            text,
            ScrollingGroup(
                _select("tz_preset", "zones", on_timezone_preset),
                id="tz_preset_scroll",
                width=1,
                height=10,
            ),
            Button(
                Format("{btn_search}"),
                id="tz_search",
                on_click=lambda callback, button, manager: manager.switch_to(
                    TenantsSG.timezone_search
                ),
            ),
            _back(TenantsSG.settings, "tz_back"),
            state=TenantsSG.timezone_presets,
            getter=timezone_presets_getter,
        ),
        Window(
            text,
            TextInput(id="tz_query", on_success=on_timezone_search, on_error=on_invalid),
            _back(TenantsSG.timezone_presets, "tz_search_back"),
            state=TenantsSG.timezone_search,
            getter=lambda dialog_manager, **kwargs: {
                "text": compose(
                    dialog_manager, _("Введите город, IANA zone или смещение (например +2)")
                ),
                **nav_texts(),
            },
        ),
        Window(
            text,
            ScrollingGroup(
                _select("tz_result", "zones", on_timezone_result),
                id="tz_result_scroll",
                width=1,
                height=10,
            ),
            Button(Format("{btn_next}"), id="tz_next", on_click=on_timezone_next, when="has_next"),
            _back(TenantsSG.timezone_presets, "tz_results_back"),
            state=TenantsSG.timezone_results,
            getter=timezone_results_getter,
        ),
        Window(
            text,
            TextInput(
                id="t_chat_id", type_factory=parse_chat_id, on_success=on_chat, on_error=on_invalid
            ),
            _back(TenantsSG.settings, "t_chat_back"),
            state=TenantsSG.chat_input,
            getter=prompt_getter,
        ),
        Window(
            text,
            Button(Format("{btn_yes}"), id="t_yes", on_click=on_confirm),
            _back(TenantsSG.card, "t_no"),
            state=TenantsSG.confirm,
            getter=confirm_getter,
        ),
    )
