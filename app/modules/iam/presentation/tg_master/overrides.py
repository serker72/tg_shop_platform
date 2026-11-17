"""Personal permissions (overrides) of a staff member (task 06).

Started from the staff card with ``start_data={"user_id": <telegram id>}``. Every getter
and handler re-checks the global ``roles.manage`` on the server (``when`` hides buttons
only at render time) and every shop id taken from a callback goes through
:func:`load_tenant` (the actor's scope). Mutations go through :class:`AccessAdminService`
(``upsert_override`` / ``remove_override``): it re-checks the right, forbids self-grants,
privilege escalation and tenant overrides of global permissions.

Flow of "Add": module -> permission -> allow/deny -> scope (skipped for
``GLOBAL_PERMISSIONS``: always "all shops") -> expiry (calendar without past days, the end
of the chosen day 23:59:59 UTC, or "permanent") -> reason (optional) -> confirm. "Edit"
reuses the flow from allow/deny with the slot (permission + scope) fixed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from operator import itemgetter
from typing import Any, Final

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import (
    Button,
    Calendar,
    CalendarConfig,
    Cancel,
    Column,
    Group,
    ScrollingGroup,
    Select,
    SwitchTo,
)
from aiogram_dialog.widgets.kbd.calendar_kbd import CalendarUserConfig
from aiogram_dialog.widgets.text import Format

from app.core.i18n import gettext as _
from app.modules.iam.admin_service import AccessAdminService, TenantAdminService
from app.modules.iam.permissions import GLOBAL_PERMISSIONS, ROLES_MANAGE
from app.modules.iam.presentation.tg_master.common import (
    load_tenant,
    snapshot_of,
)
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries, OverrideRow
from app.modules.iam.presentation.tg_master.roles import MODULES, PERMISSION_NAMES, module_titles
from app.modules.iam.presentation.tg_master.staff import parse_telegram_id
from app.modules.iam.presentation.tg_master.states import OverridesSG
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

__all__ = ["REASON_MAX_LENGTH", "end_of_day", "overrides_dialog", "parse_reason"]

PAGE_SIZE: Final = 10
REASON_MAX_LENGTH: Final = 500
#: Keeps a window text within the 4096 characters of a Telegram message.
TEXT_LIMIT: Final = 3800
DRAFT: Final = "draft"


def end_of_day(day: date) -> datetime:
    """Expiry of an override chosen in the calendar: 23:59:59 UTC of that day."""
    return datetime.combine(day, time(23, 59, 59), tzinfo=UTC)


def _today() -> date:
    return datetime.now(UTC).date()


def parse_reason(text: str) -> str:
    reason = text.strip()
    if not 1 <= len(reason) <= REASON_MAX_LENGTH:
        raise ValueError("invalid reason")
    return reason


class FutureCalendar(Calendar):
    """Calendar in UTC that renders no day before today (the handler re-checks the date:
    a forged callback may still carry a past one)."""

    async def _get_user_config(
        self, data: dict[str, Any], manager: DialogManager
    ) -> CalendarUserConfig:
        return CalendarUserConfig(min_date=_today())


# --- access and helpers ----------------------------------------------------------------


def _allowed(manager: DialogManager) -> bool:
    return snapshot_of(manager).decide(ROLES_MANAGE).allowed


async def _guard(event: CallbackQuery | Message, manager: DialogManager) -> bool:
    if _allowed(manager):
        return True
    if isinstance(event, CallbackQuery):
        await event.answer(denied_text())
    await manager.done()
    return False


def _target(manager: DialogManager) -> int | None:
    start = manager.start_data if isinstance(manager.start_data, dict) else {}
    try:
        return parse_telegram_id(str(start.get("user_id", "")))
    except ValueError:
        return None


async def _user(manager: DialogManager) -> Any:
    target = _target(manager)
    if target is None:
        return None
    return await (await resolve(manager, IamAdminQueries)).user(target)


def _draft(manager: DialogManager) -> dict[str, Any]:
    draft: dict[str, Any] = manager.dialog_data.setdefault(DRAFT, {})
    return draft


def _labels() -> dict[str, Any]:
    return {
        "btn_add": _("Добавить персональное право"),
        "btn_edit": _("Изменить"),
        "btn_allow": _("Разрешить"),
        "btn_deny": _("Запретить"),
        "btn_all": _("Все магазины"),
        "btn_forever": _("Бессрочно"),
        "btn_skip": _("Пропустить"),
        "btn_save": _("Сохранить"),
        **nav_texts(),
    }


_EMPTY: Final[dict[str, Any]] = {
    "items": [],
    "modules": [],
    "perms": [],
    "tenants": [],
    "ok": False,
}


def _page(manager: DialogManager, *lines: str, **data: Any) -> dict[str, Any]:
    text = compose(manager, *lines)
    if len(text) > TEXT_LIMIT:
        text = text[: TEXT_LIMIT - 1] + "…"
    return {**_EMPTY, **_labels(), **data, "text": text}


def _key(row: OverrideRow) -> str:
    """Button id of an override: its row id (a slug + shop id may exceed the 64 bytes of
    Telegram ``callback_data``)."""
    return str(row.override.id)


def _effect(granted: bool) -> str:
    return _("разрешено") if granted else _("запрещено")


def _expiry(expires_at: datetime | None) -> str:
    if expires_at is None:
        return _("бессрочно")
    return expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _perm_name(slug: str) -> str:
    return PERMISSION_NAMES.get(slug, slug)


async def _scope_name(manager: DialogManager, tenant_id: Any) -> str | None:
    """Scope label: all shops, a shop name in the actor's scope, ``None`` out of scope."""
    if tenant_id is None or tenant_id == "":
        return _("все магазины")
    found = await load_tenant(manager, tenant_id)
    return None if found is None else found[0].name


async def _visible_rows(
    manager: DialogManager, user: Any
) -> tuple[list[tuple[OverrideRow, str]], int]:
    """Overrides of the user with their scope name; shop rows out of scope are counted only."""
    queries = await resolve(manager, IamAdminQueries)
    visible, hidden = [], 0
    for row in await queries.user_overrides(user.id):
        scope = await _scope_name(manager, row.override.tenant_id)
        if scope is None:
            hidden += 1
        else:
            visible.append((row, scope))
    return visible, hidden


def _describe(row: OverrideRow, scope: str) -> str:
    granter = "—"
    if row.granted_by_telegram_id is not None:
        name = f"@{row.granted_by_username} " if row.granted_by_username else ""
        granter = f"{name}({row.granted_by_telegram_id})"
    parts = [
        f"{'✅' if row.override.is_granted else '⛔'} {_perm_name(row.permission_slug)}",
        _effect(row.override.is_granted),
        scope,
        _("до {when}").format(when=_expiry(row.override.expires_at))
        if row.override.expires_at
        else _expiry(None),
        _("выдал: {who}").format(who=granter),
    ]
    if row.override.reason:
        parts.append(_("причина: {reason}").format(reason=row.override.reason))
    return "; ".join(parts)


async def _no_user(manager: DialogManager) -> dict[str, Any]:
    return _page(manager, error_text("user_not_found"))


# --- getters ---------------------------------------------------------------------------


async def list_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _page(dialog_manager, denied_text())
    user = await _user(dialog_manager)
    if user is None:
        return await _no_user(dialog_manager)
    rows, hidden = await _visible_rows(dialog_manager, user)
    active = [(r, s) for r, s in rows if not r.is_expired]
    expired = [(r, s) for r, s in rows if r.is_expired]
    lines = [_("Персональные права сотрудника {id}").format(id=user.telegram_user_id)]
    lines.append(_("Действующие:"))
    lines.extend([f"• {_describe(r, s)}" for r, s in active] or ["—"])
    lines.append(_("Истёкшие:"))
    lines.extend([f"• {_describe(r, s)}" for r, s in expired] or ["—"])
    if hidden:
        lines.append(_("Ещё в магазинах вне вашей области: {count}").format(count=hidden))
    items = [
        (
            _key(r),
            short(("⌛ " if r.is_expired else "") + f"{_perm_name(r.permission_slug)} · {s}"),
        )
        for r, s in rows
    ]
    return _page(dialog_manager, *lines, items=items, ok=True)


async def _selected(manager: DialogManager) -> tuple[OverrideRow, str] | None:
    user = await _user(manager)
    if user is None:
        return None
    rows, _hidden = await _visible_rows(manager, user)
    key = str(manager.dialog_data.get("key", ""))
    return next(((r, s) for r, s in rows if _key(r) == key), None)


async def item_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _page(dialog_manager, denied_text())
    found = await _selected(dialog_manager)
    if found is None:
        return _page(dialog_manager, error_text("override_not_found"))
    row, scope = found
    status = _("истекло") if row.is_expired else _("действует")
    return _page(
        dialog_manager,
        f"{_perm_name(row.permission_slug)} ({row.permission_slug})",
        _("Статус: {status}").format(status=status),
        _describe(row, scope),
        ok=True,
    )


async def module_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _page(dialog_manager, denied_text())
    titles = module_titles()
    return _page(
        dialog_manager,
        _("Выберите модуль."),
        modules=[(m, titles.get(m, m)) for m in MODULES],
        ok=True,
    )


async def permission_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _page(dialog_manager, denied_text())
    module = str(_draft(dialog_manager).get("module", ""))
    slugs = MODULES.get(module, ())
    return _page(
        dialog_manager,
        _("Выберите право."),
        perms=[(s, _perm_name(s)) for s in slugs],
        ok=True,
    )


async def _summary(manager: DialogManager) -> list[str]:
    draft = _draft(manager)
    slug = str(draft.get("slug", ""))
    lines = [_("Право: {name}").format(name=_perm_name(slug))]
    if "granted" in draft:
        lines.append(_("Действие: {effect}").format(effect=_effect(bool(draft["granted"]))))
    if "tenant" in draft:
        scope = await _scope_name(manager, draft["tenant"])
        lines.append(_("Область: {scope}").format(scope=scope or "—"))
    if "expires" in draft:
        expires = draft["expires"]
        lines.append(
            _("Срок: {when}").format(
                when=_expiry(datetime.fromisoformat(expires) if expires else None)
            )
        )
    if draft.get("reason"):
        lines.append(_("Причина: {reason}").format(reason=draft["reason"]))
    return lines


async def step_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _page(dialog_manager, denied_text())
    prompts = {
        OverridesSG.effect: _("Разрешить или запретить это право?"),
        OverridesSG.expires: _(
            "Выберите последний день действия (до 23:59:59 UTC) или «Бессрочно»."
        ),
        OverridesSG.reason: _("Введите причину (до 500 символов) или нажмите «Пропустить»."),
        OverridesSG.confirm: _("Сохранить персональное право?"),
        OverridesSG.delete: _("Удалить персональное право?"),
    }
    state = dialog_manager.current_context().state
    return _page(
        dialog_manager, *await _summary(dialog_manager), "", prompts.get(state, ""), ok=True
    )


async def scope_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _page(dialog_manager, denied_text())
    service = await resolve(dialog_manager, TenantAdminService)
    tenants = await service.list_tenants(actor_id(dialog_manager))
    return _page(
        dialog_manager,
        *await _summary(dialog_manager),
        "",
        _("Где действует право: во всех магазинах или в одном?"),
        tenants=[(str(t.id), short(f"{t.name} ({t.slug})")) for t in tenants],
        ok=True,
    )


# --- handlers --------------------------------------------------------------------------


async def on_item(callback: CallbackQuery, widget: Any, manager: DialogManager, item: str) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data["key"] = item
    await manager.switch_to(OverridesSG.item)


async def on_add(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data[DRAFT] = {}
    await manager.switch_to(OverridesSG.module)


async def on_edit(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    found = await _selected(manager)
    if found is None:
        set_notice(manager, error_text("override_not_found"))
        await manager.switch_to(OverridesSG.list)
        return
    row, _scope = found
    tenant_id = row.override.tenant_id
    manager.dialog_data[DRAFT] = {
        "slug": row.permission_slug,
        "tenant": "" if tenant_id is None else str(tenant_id),
        "edit": True,
    }
    await manager.switch_to(OverridesSG.effect)


async def on_module(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item: str
) -> None:
    if not await _guard(callback, manager) or item not in MODULES:
        return
    manager.dialog_data[DRAFT] = {"module": item}
    await manager.switch_to(OverridesSG.permission)


async def on_permission(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item: str
) -> None:
    if not await _guard(callback, manager) or item not in PERMISSION_NAMES:
        return
    draft = _draft(manager)
    draft["slug"] = item
    if item in GLOBAL_PERMISSIONS:
        draft["tenant"] = ""
    await manager.switch_to(OverridesSG.effect)


async def on_effect(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    draft = _draft(manager)
    if str(draft.get("slug", "")) not in PERMISSION_NAMES:
        await manager.switch_to(OverridesSG.list)
        return
    draft["granted"] = button.widget_id == "ov_allow"
    await manager.switch_to(OverridesSG.expires if "tenant" in draft else OverridesSG.scope)


async def on_all_shops(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    _draft(manager)["tenant"] = ""
    await manager.switch_to(OverridesSG.expires)


async def on_shop(callback: CallbackQuery, widget: Any, manager: DialogManager, item: str) -> None:
    if not await _guard(callback, manager):
        return
    draft = _draft(manager)
    found = await load_tenant(manager, item)
    if found is None or found[0].is_deleted:
        set_notice(manager, error_text("tenant_not_found"))
        return
    if str(draft.get("slug", "")) in GLOBAL_PERMISSIONS:
        set_notice(manager, error_text("global_permission_tenant_override"))
        return
    draft["tenant"] = str(found[0].id)
    await manager.switch_to(OverridesSG.expires)


async def on_date(callback: CallbackQuery, widget: Any, manager: DialogManager, day: date) -> None:
    if not await _guard(callback, manager):
        return
    if day < _today():
        # Forged callback: the calendar renders no past day.
        set_notice(manager, error_text("expires_at_not_in_future"))
        return
    _draft(manager)["expires"] = end_of_day(day).isoformat()
    await manager.switch_to(OverridesSG.reason)


async def on_forever(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    _draft(manager)["expires"] = ""
    await manager.switch_to(OverridesSG.reason)


async def on_reason(message: Message, widget: Any, manager: DialogManager, value: str) -> None:
    if not await _guard(message, manager):
        return
    _draft(manager)["reason"] = value
    await manager.switch_to(OverridesSG.confirm)


async def on_invalid_reason(
    message: Message, widget: Any, manager: DialogManager, error: Any
) -> None:
    set_notice(manager, _("Причина: от 1 до {max} символов.").format(max=REASON_MAX_LENGTH))


async def on_skip_reason(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    _draft(manager)["reason"] = ""
    await manager.switch_to(OverridesSG.confirm)


async def on_save(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    target = _target(manager)
    draft = _draft(manager)
    slug = str(draft.get("slug", ""))
    if target is None or slug not in PERMISSION_NAMES or "granted" not in draft:
        set_notice(manager, error_text("override_not_found"))
        await manager.switch_to(OverridesSG.list)
        return
    tenant_id = None
    if draft.get("tenant"):
        found = await load_tenant(manager, draft["tenant"])
        if found is None or found[0].is_deleted:
            set_notice(manager, error_text("tenant_not_found"))
            return
        tenant_id = found[0].id
    expires = str(draft.get("expires") or "")
    expires_at = datetime.fromisoformat(expires) if expires else None
    if expires_at is not None and expires_at <= datetime.now(UTC):
        set_notice(manager, error_text("expires_at_not_in_future"))
        return
    reason = str(draft.get("reason") or "") or None
    actor, granted = actor_id(manager), bool(draft["granted"])
    outcome = await mutate(
        manager,
        AccessAdminService,
        lambda s: s.upsert_override(
            actor,
            target,
            slug,
            is_granted=granted,
            tenant_id=tenant_id,
            expires_at=expires_at,
            reason=reason,
        ),
    )
    if outcome.ok:
        manager.dialog_data.pop(DRAFT, None)
        await manager.switch_to(OverridesSG.list)


async def on_ask_delete(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    found = await _selected(manager)
    if found is None:
        set_notice(manager, error_text("override_not_found"))
        await manager.switch_to(OverridesSG.list)
        return
    row, _scope = found
    tenant_id = row.override.tenant_id
    manager.dialog_data[DRAFT] = {
        "slug": row.permission_slug,
        "tenant": "" if tenant_id is None else str(tenant_id),
    }
    await manager.switch_to(OverridesSG.delete)


async def on_delete(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    found = await _selected(manager)
    target = _target(manager)
    if found is None or target is None:
        set_notice(manager, error_text("override_not_found"))
        await manager.switch_to(OverridesSG.list)
        return
    row, _scope = found
    actor, slug, tenant_id = actor_id(manager), row.permission_slug, row.override.tenant_id
    await mutate(
        manager,
        AccessAdminService,
        lambda s: s.remove_override(actor, target, slug, tenant_id=tenant_id),
    )
    manager.dialog_data.pop(DRAFT, None)
    await manager.switch_to(OverridesSG.list)


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


def overrides_dialog() -> Dialog:
    """New :class:`Dialog` instance (a dialog router can be included only once)."""
    text = Format("{text}")
    return Dialog(
        Window(
            text,
            ScrollingGroup(
                _select("ov_sel", "items", on_item),
                id="ov_scroll",
                width=1,
                height=PAGE_SIZE,
                hide_on_single_page=True,
            ),
            Button(Format("{btn_add}"), id="ov_add", on_click=on_add, when="ok"),
            Cancel(Format("{btn_back}")),
            state=OverridesSG.list,
            getter=list_getter,
        ),
        Window(
            text,
            Column(
                Button(Format("{btn_edit}"), id="ov_edit", on_click=on_edit, when="ok"),
                Button(Format("{btn_delete}"), id="ov_del", on_click=on_ask_delete, when="ok"),
                _back(OverridesSG.list, "ov_item_back"),
            ),
            state=OverridesSG.item,
            getter=item_getter,
        ),
        Window(
            text,
            Group(_select("ov_mod", "modules", on_module), width=2),
            _back(OverridesSG.list, "ov_mod_back"),
            state=OverridesSG.module,
            getter=module_getter,
        ),
        Window(
            text,
            Column(_select("ov_perm", "perms", on_permission)),
            _back(OverridesSG.module, "ov_perm_back"),
            state=OverridesSG.permission,
            getter=permission_getter,
        ),
        Window(
            text,
            Group(
                Button(Format("{btn_allow}"), id="ov_allow", on_click=on_effect),
                Button(Format("{btn_deny}"), id="ov_deny", on_click=on_effect),
                width=2,
                when="ok",
            ),
            _back(OverridesSG.list, "ov_eff_back"),
            state=OverridesSG.effect,
            getter=step_getter,
        ),
        Window(
            text,
            Button(Format("{btn_all}"), id="ov_all", on_click=on_all_shops, when="ok"),
            ScrollingGroup(
                _select("ov_shop", "tenants", on_shop),
                id="ov_shop_scroll",
                width=1,
                height=PAGE_SIZE,
                hide_on_single_page=True,
            ),
            _back(OverridesSG.list, "ov_scope_back"),
            state=OverridesSG.scope,
            getter=scope_getter,
        ),
        Window(
            text,
            FutureCalendar(
                id="ov_cal",
                on_click=on_date,  # type: ignore[arg-type]
                config=CalendarConfig(timezone=UTC),
                when="ok",
            ),
            Button(Format("{btn_forever}"), id="ov_forever", on_click=on_forever, when="ok"),
            _back(OverridesSG.list, "ov_exp_back"),
            state=OverridesSG.expires,
            getter=step_getter,
        ),
        Window(
            text,
            TextInput(
                id="ov_reason_in",
                type_factory=parse_reason,
                on_success=on_reason,
                on_error=on_invalid_reason,
            ),
            Button(Format("{btn_skip}"), id="ov_skip", on_click=on_skip_reason, when="ok"),
            _back(OverridesSG.list, "ov_reason_back"),
            state=OverridesSG.reason,
            getter=step_getter,
        ),
        Window(
            text,
            Button(Format("{btn_save}"), id="ov_save", on_click=on_save, when="ok"),
            _back(OverridesSG.list, "ov_confirm_back"),
            state=OverridesSG.confirm,
            getter=step_getter,
        ),
        Window(
            text,
            Button(Format("{btn_yes}"), id="ov_yes", on_click=on_delete, when="ok"),
            _back(OverridesSG.item, "ov_del_back"),
            state=OverridesSG.delete,
            getter=step_getter,
        ),
    )
