"""Roles dialog (task 06): list, create, card, rename, (de)activation, delete, permissions.

Every getter and handler re-checks the global ``roles.manage`` on the server: ``when`` of
aiogram-dialog hides a button only at render time, a forged ``callback_data`` still reaches
the handler. Mutations go through :class:`RoleAdminService` (it re-checks the right,
protects system roles and forbids privilege escalation). System roles are shown read-only:
only the display name may be changed (the service allows ``rename``).

Permissions are edited with a ``Multiselect`` grouped by module (``{module}.{action}``):
the window shows module buttons and the checkboxes of the selected module; the checked set
lives in the widget until "Save" calls ``set_permissions`` with the exact set.
"""

from __future__ import annotations

from collections import defaultdict
from operator import itemgetter
from typing import Any, Final, cast

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import (
    Button,
    Cancel,
    Column,
    Group,
    ManagedMultiselect,
    Multiselect,
    ScrollingGroup,
    Select,
    SwitchTo,
)
from aiogram_dialog.widgets.text import Format

from app.core.i18n import gettext as _
from app.modules.iam.admin_service import RoleAdminService, validate_role_slug
from app.modules.iam.permissions import ROLES_MANAGE, SEED_PERMISSIONS
from app.modules.iam.presentation.tg_master.common import (
    actor_id,
    compose,
    denied_text,
    error_text,
    mutate,
    nav_texts,
    parse_uuid,
    resolve,
    set_notice,
    short,
    snapshot_of,
)
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.iam.presentation.tg_master.states import RolesSG
from app.modules.iam.presentation.tg_master.tenants import parse_name
from app.shared_kernel.domain.exceptions import DomainError

__all__ = ["MODULES", "PERMISSION_NAMES", "module_of", "parse_role_slug", "roles_dialog"]

PAGE_SIZE: Final = 10
PERMS_WIDGET: Final = "r_perms"
#: Permission slug -> Russian description of the catalogue (32 rows).
PERMISSION_NAMES: Final[dict[str, str]] = dict(SEED_PERMISSIONS)


def module_of(slug: str) -> str:
    return slug.split(".", 1)[0]


def _group_modules() -> dict[str, tuple[str, ...]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for slug, _name in SEED_PERMISSIONS:
        groups[module_of(slug)].append(slug)
    return {module: tuple(slugs) for module, slugs in groups.items()}


#: Module -> its permission slugs, in the catalogue order.
MODULES: Final[dict[str, tuple[str, ...]]] = _group_modules()


def module_titles() -> dict[str, str]:
    return {
        "products": _("Товары"),
        "orders": _("Заказы"),
        "cases": _("Обращения"),
        "users": _("Пользователи"),
        "referrals": _("Рефералы"),
        "reviews": _("Отзывы"),
        "preorders": _("Предзаказы"),
        "payouts": _("Выплаты"),
        "accounting": _("Бухгалтерия"),
        "statistics": _("Статистика"),
        "moderators": _("Модераторы"),
        "roles": _("Роли"),
        "settings": _("Настройки"),
        "tenants": _("Магазины"),
        "proxies": _("Прокси"),
        "logs": _("Логи"),
        "bots": _("Боты"),
    }


def parse_role_slug(text: str) -> str:
    """The service rule (``validate_role_slug``) as a ``TextInput`` type factory."""
    slug = text.strip()
    try:
        validate_role_slug(slug)
    except DomainError as error:
        raise ValueError(str(error)) from error
    return slug


# --- access and helpers ----------------------------------------------------------------


def _allowed(manager: DialogManager) -> bool:
    return snapshot_of(manager).decide(ROLES_MANAGE).allowed


async def _guard(event: CallbackQuery | Message, manager: DialogManager) -> bool:
    """Handler gate: without ``roles.manage`` close the dialog with a notice."""
    if _allowed(manager):
        return True
    if isinstance(event, CallbackQuery):
        await event.answer(denied_text())
    await manager.done()
    return False


def _labels() -> dict[str, Any]:
    return {
        "btn_create": _("Создать роль"),
        "btn_perms": _("Права роли"),
        "btn_rename": _("Переименовать"),
        "btn_save": _("Сохранить"),
        **nav_texts(),
    }


_EMPTY: Final[dict[str, Any]] = {
    "roles": [],
    "modules": [],
    "perms": [],
    "has_role": False,
    "editable": False,
    "can_activate": False,
    "can_deactivate": False,
    "can_delete": False,
}


def _page(manager: DialogManager, *lines: str, **data: Any) -> dict[str, Any]:
    return {**_EMPTY, **_labels(), **data, "text": compose(manager, *lines)}


def _denied(manager: DialogManager) -> dict[str, Any]:
    return _page(manager, denied_text())


async def _current_role(manager: DialogManager) -> Any:
    role_id = parse_uuid(manager.dialog_data.get("role_id"))
    if role_id is None:
        return None
    return await (await resolve(manager, IamAdminQueries)).role(role_id)


def _role_label(role: Any) -> str:
    prefix = "🔒 " if role.is_system else ""
    suffix = "" if role.is_active else " ⏸"
    return short(f"{prefix}{role.name} ({role.slug})") + suffix


def _picker(manager: DialogManager) -> ManagedMultiselect[str]:
    return cast("ManagedMultiselect[str]", manager.find(PERMS_WIDGET))


async def _editable(manager: DialogManager) -> frozenset[str]:
    """Permissions edited by the checkboxes: catalogue slugs that are live in the database.
    The rest of the role's set (inactive or unknown here) is kept as is on save."""
    live = await (await resolve(manager, IamAdminQueries)).live_permission_slugs()
    return frozenset(PERMISSION_NAMES) & live


def _current_module(manager: DialogManager) -> str:
    module = str(manager.dialog_data.get("module", ""))
    return module if module in MODULES else next(iter(MODULES))


# --- getters ---------------------------------------------------------------------------


async def list_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _denied(dialog_manager)
    roles = await (await resolve(dialog_manager, IamAdminQueries)).roles()
    return _page(
        dialog_manager,
        _("Роли: {count}").format(count=len(roles)),
        _("🔒 — системная роль (набор прав только для просмотра), ⏸ — деактивирована."),
        roles=[(str(r.id), _role_label(r)) for r in roles],
    )


async def card_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _denied(dialog_manager)
    role = await _current_role(dialog_manager)
    if role is None:
        return _page(dialog_manager, error_text("role_not_found"))
    queries = await resolve(dialog_manager, IamAdminQueries)
    count = len(await queries.role_permission_slugs(role.id))
    lines = [
        _("Роль: {name}").format(name=role.name),
        _("Slug: {slug}").format(slug=role.slug),
        _("Статус: {status}").format(
            status=_("активна") if role.is_active else _("деактивирована")
        ),
        _("Прав: {count} из {total}").format(count=count, total=len(PERMISSION_NAMES)),
    ]
    if role.is_system:
        lines.append(_("Системная роль: набор прав только для просмотра."))
    custom = not role.is_system
    return _page(
        dialog_manager,
        *lines,
        has_role=True,
        can_activate=custom and not role.is_active,
        can_deactivate=custom and role.is_active,
        can_delete=custom,
    )


async def permissions_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _denied(dialog_manager)
    role = await _current_role(dialog_manager)
    if role is None:
        return _page(dialog_manager, error_text("role_not_found"))
    titles = module_titles()
    if role.is_system:
        queries = await resolve(dialog_manager, IamAdminQueries)
        held = await queries.role_permission_slugs(role.id)
        lines = [
            _("Права системной роли «{name}» (только просмотр): {count} из {total}").format(
                name=role.name, count=len(held), total=len(PERMISSION_NAMES)
            )
        ]
        for module, slugs in MODULES.items():
            names = [PERMISSION_NAMES[s] for s in slugs if s in held]
            if names:
                lines.append(f"\n{titles.get(module, module)}:")
                lines.extend(f"• {name}" for name in names)
        return _page(dialog_manager, *lines, has_role=True)
    checked = set(_picker(dialog_manager).get_checked())
    editable = await _editable(dialog_manager)
    queries = await resolve(dialog_manager, IamAdminQueries)
    fixed = sorted(await queries.role_permission_slugs(role.id) - editable)
    fixed_lines = (
        [
            _("Не редактируются здесь (неактивные или вне справочника), сохраняются как есть:"),
            *(f"• {PERMISSION_NAMES.get(s, s)} ({s})" for s in fixed),
        ]
        if fixed
        else []
    )
    current = _current_module(dialog_manager)
    modules = []
    for module, slugs in MODULES.items():
        marker = "» " if module == current else ""
        picked = sum(1 for s in slugs if s in checked)
        modules.append((module, f"{marker}{titles.get(module, module)} {picked}/{len(slugs)}"))
    return _page(
        dialog_manager,
        _("Права роли «{name}»: отмечено {count} из {total}.").format(
            name=role.name,
            count=len(checked & PERMISSION_NAMES.keys()),
            total=len(PERMISSION_NAMES),
        ),
        _("Модуль: {module}. Отметьте права и нажмите «Сохранить».").format(
            module=titles.get(current, current)
        ),
        *fixed_lines,
        has_role=True,
        editable=True,
        modules=modules,
        perms=[(slug, PERMISSION_NAMES[slug]) for slug in MODULES[current] if slug in editable],
    )


async def prompt_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _denied(dialog_manager)
    prompts = {
        RolesSG.create_name: _("Введите название новой роли (до 128 символов)."),
        RolesSG.create_slug: _(
            "Введите slug роли: латиница в нижнем регистре, цифры и «_», начинается с буквы."
        ),
        RolesSG.rename: _("Введите новое название роли."),
    }
    state = dialog_manager.current_context().state
    return _page(dialog_manager, prompts.get(state, ""))


async def confirm_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _denied(dialog_manager)
    questions = {
        "delete": _("Удалить роль? Она будет снята со всех сотрудников."),
        "deactivate": _("Деактивировать роль? Её права перестанут действовать."),
    }
    action = str(dialog_manager.dialog_data.get("confirm", ""))
    return _page(dialog_manager, questions.get(action, ""))


# --- handlers --------------------------------------------------------------------------


async def on_role(callback: CallbackQuery, widget: Any, manager: DialogManager, item: str) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data["role_id"] = item
    await manager.switch_to(RolesSG.card)


async def _role_or_back(event: CallbackQuery | Message, manager: DialogManager) -> Any:
    if not await _guard(event, manager):
        return None
    role = await _current_role(manager)
    if role is None:
        set_notice(manager, error_text("role_not_found"))
        await manager.switch_to(RolesSG.list)
    return role


async def on_open_permissions(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    role = await _role_or_back(callback, manager)
    if role is None:
        return
    picker = _picker(manager)
    await picker.reset_checked()  # type: ignore[no-untyped-call]
    if not role.is_system:
        queries = await resolve(manager, IamAdminQueries)
        for slug in sorted(await queries.role_permission_slugs(role.id)):
            await picker.set_checked(slug, True)
    manager.dialog_data["module"] = next(iter(MODULES))
    await manager.switch_to(RolesSG.permissions)


async def on_toggle(event: Any, widget: Any, manager: DialogManager, item: str) -> None:
    """A forged checkbox click without ``roles.manage`` closes the dialog (state is lost)."""
    await _guard(event, manager)


async def on_module(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item: str
) -> None:
    if await _guard(callback, manager) and item in MODULES:
        manager.dialog_data["module"] = item


async def on_save(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    role = await _role_or_back(callback, manager)
    if role is None:
        return
    if role.is_system:
        # Forged callback: the button is not rendered for system roles.
        set_notice(manager, error_text("system_role_protected"))
        return
    editable = await _editable(manager)
    held = await (await resolve(manager, IamAdminQueries)).role_permission_slugs(role.id)
    checked = frozenset(_picker(manager).get_checked())
    # Non-editable permissions (inactive / outside the catalogue) are kept as they are.
    slugs = (checked & editable) | (held - editable)
    actor, role_id = actor_id(manager), role.id
    outcome = await mutate(
        manager, RoleAdminService, lambda s: s.set_permissions(actor, role_id, slugs)
    )
    if outcome.ok:
        await manager.switch_to(RolesSG.card)


async def on_invalid_name(
    message: Message, widget: Any, manager: DialogManager, error: Any
) -> None:
    set_notice(manager, _("Название: от 1 до 128 символов."))


async def on_invalid_slug(
    message: Message, widget: Any, manager: DialogManager, error: Any
) -> None:
    set_notice(manager, error_text("invalid_role_slug"))


async def on_new_name(message: Message, widget: Any, manager: DialogManager, value: str) -> None:
    if not await _guard(message, manager):
        return
    manager.dialog_data["new_name"] = value
    await manager.switch_to(RolesSG.create_slug)


async def on_new_slug(message: Message, widget: Any, manager: DialogManager, value: str) -> None:
    if not await _guard(message, manager):
        return
    name = str(manager.dialog_data.get("new_name", ""))
    if not name:
        await manager.switch_to(RolesSG.create_name)
        return
    actor = actor_id(manager)
    outcome = await mutate(
        manager, RoleAdminService, lambda s: s.create(actor, name=name, slug=value)
    )
    if outcome.ok and outcome.value is not None:
        manager.dialog_data.pop("new_name", None)
        manager.dialog_data["role_id"] = str(outcome.value.id)
        await manager.switch_to(RolesSG.card)


async def on_rename(message: Message, widget: Any, manager: DialogManager, value: str) -> None:
    role = await _role_or_back(message, manager)
    if role is None:
        return
    actor, role_id = actor_id(manager), role.id
    outcome = await mutate(manager, RoleAdminService, lambda s: s.rename(actor, role_id, value))
    if outcome.ok:
        await manager.switch_to(RolesSG.card)


async def on_activate(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    role = await _role_or_back(callback, manager)
    if role is not None:
        actor, role_id = actor_id(manager), role.id
        await mutate(manager, RoleAdminService, lambda s: s.activate(actor, role_id))


async def on_ask(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data["confirm"] = button.widget_id
    await manager.switch_to(RolesSG.confirm)


async def on_confirm(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    action = manager.dialog_data.pop("confirm", "")
    role = await _role_or_back(callback, manager)
    if role is None:
        return
    actor, role_id = actor_id(manager), role.id
    if action == "delete":
        outcome = await mutate(manager, RoleAdminService, lambda s: s.soft_delete(actor, role_id))
        if outcome.ok:
            await manager.switch_to(RolesSG.list)
            return
    elif action == "deactivate":
        await mutate(manager, RoleAdminService, lambda s: s.deactivate(actor, role_id))
    await manager.switch_to(RolesSG.card)


# --- dialog ----------------------------------------------------------------------------


def _back(state: Any, widget_id: str) -> SwitchTo:
    return SwitchTo(Format("{btn_back}"), id=widget_id, state=state)


def roles_dialog() -> Dialog:
    """New :class:`Dialog` instance (a dialog router can be included only once)."""
    text = Format("{text}")
    return Dialog(
        Window(
            text,
            ScrollingGroup(
                Select(
                    Format("{item[1]}"),
                    id="r_sel",
                    item_id_getter=itemgetter(0),
                    items="roles",
                    on_click=on_role,
                ),
                id="r_scroll",
                width=1,
                height=PAGE_SIZE,
                hide_on_single_page=True,
            ),
            SwitchTo(Format("{btn_create}"), id="r_create", state=RolesSG.create_name),
            Cancel(Format("{btn_back}")),
            state=RolesSG.list,
            getter=list_getter,
        ),
        Window(
            text,
            TextInput(
                id="r_name",
                type_factory=parse_name,
                on_success=on_new_name,
                on_error=on_invalid_name,
            ),
            _back(RolesSG.list, "r_name_back"),
            state=RolesSG.create_name,
            getter=prompt_getter,
        ),
        Window(
            text,
            TextInput(
                id="r_slug",
                type_factory=parse_role_slug,
                on_success=on_new_slug,
                on_error=on_invalid_slug,
            ),
            _back(RolesSG.list, "r_slug_back"),
            state=RolesSG.create_slug,
            getter=prompt_getter,
        ),
        Window(
            text,
            Column(
                Button(
                    Format("{btn_perms}"),
                    id="r_perms_open",
                    on_click=on_open_permissions,
                    when="has_role",
                ),
                SwitchTo(
                    Format("{btn_rename}"), id="r_rename", state=RolesSG.rename, when="has_role"
                ),
                Button(
                    Format("{btn_deactivate}"),
                    id="deactivate",
                    on_click=on_ask,
                    when="can_deactivate",
                ),
                Button(
                    Format("{btn_activate}"),
                    id="r_activate",
                    on_click=on_activate,
                    when="can_activate",
                ),
                Button(Format("{btn_delete}"), id="delete", on_click=on_ask, when="can_delete"),
                _back(RolesSG.list, "r_card_back"),
            ),
            state=RolesSG.card,
            getter=card_getter,
        ),
        Window(
            text,
            TextInput(
                id="r_rename_in",
                type_factory=parse_name,
                on_success=on_rename,
                on_error=on_invalid_name,
            ),
            _back(RolesSG.card, "r_rename_back"),
            state=RolesSG.rename,
            getter=prompt_getter,
        ),
        Window(
            text,
            Group(
                Select(
                    Format("{item[1]}"),
                    id="r_mod",
                    item_id_getter=itemgetter(0),
                    items="modules",
                    on_click=on_module,
                ),
                width=3,
                when="editable",
            ),
            Column(
                Multiselect(
                    Format("✅ {item[1]}"),
                    Format("▫️ {item[1]}"),
                    id=PERMS_WIDGET,
                    item_id_getter=itemgetter(0),
                    items="perms",
                    on_state_changed=on_toggle,
                ),
                when="editable",
            ),
            Button(Format("{btn_save}"), id="r_save", on_click=on_save, when="editable"),
            _back(RolesSG.card, "r_perms_back"),
            state=RolesSG.permissions,
            getter=permissions_getter,
        ),
        Window(
            text,
            Button(Format("{btn_yes}"), id="r_yes", on_click=on_confirm),
            _back(RolesSG.card, "r_no"),
            state=RolesSG.confirm,
            getter=confirm_getter,
        ),
    )
