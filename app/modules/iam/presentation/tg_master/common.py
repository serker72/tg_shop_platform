"""IAM-specific helpers of the Master Bot dialogs: snapshot, read scope, IAM texts.

Generic helpers (``mutate``, ``error_text``, notices, DI lookup) live in
:mod:`app.shared_kernel.presentation.tg_master.common`; this module registers the IAM error
texts and the ``"permissions"`` stale-cache warning there on import.

``when`` of aiogram-dialog widgets works only at render time: a forged ``callback_data``
still reaches hidden buttons and any ``Select`` item id. So every getter/handler re-checks
read access on the server (:func:`load_tenant`, :func:`can_view_staff`); mutations are
checked by the admin services themselves.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from aiogram_dialog import DialogManager

from app.core.i18n import gettext as _
from app.modules.iam.models import Tenant, TenantSettings
from app.modules.iam.permissions import TENANTS_MANAGE, USERS_VIEW
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.iam.snapshot import PermissionSnapshot
from app.shared_kernel.presentation.tg_master.common import (
    register_error_texts,
    register_stale_cache_texts,
    resolve,
)

__all__ = [
    "BIGINT_MAX",
    "STAFF_VIEW_PERMISSIONS",
    "can_view_staff",
    "cache_warning",
    "in_scope",
    "load_tenant",
    "mark",
    "parse_uuid",
    "snapshot_of",
    "status_text",
    "yes_no",
]

#: Telegram ids are stored in PostgreSQL ``bigint``.
BIGINT_MAX: Final = 2**63
#: Global permissions that open the "Staff" section (list and cards of staff users);
#: the superuser passes every check of the snapshot.
STAFF_VIEW_PERMISSIONS: Final = (USERS_VIEW,)


def snapshot_of(manager: DialogManager) -> PermissionSnapshot:
    from app.entrypoints.master_bot.middlewares import SNAPSHOT_KEY

    snapshot: PermissionSnapshot = manager.middleware_data[SNAPSHOT_KEY]
    return snapshot


# --- read access -------------------------------------------------------------------------


def can_view_staff(snapshot: PermissionSnapshot) -> bool:
    return any(snapshot.decide(slug).allowed for slug in STAFF_VIEW_PERMISSIONS)


def in_scope(snapshot: PermissionSnapshot, tenant_id: UUID) -> bool:
    """Shop visible to the actor: superuser, ``tenants.view_all`` or ``tenant_moderators``."""
    return snapshot.is_superuser or snapshot.has_tenant_access(tenant_id)


def parse_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


async def load_tenant(
    manager: DialogManager, tenant_id: Any
) -> tuple[Tenant, TenantSettings | None] | None:
    """Tenant + settings if the actor may read it (deleted ones: ``tenants.manage`` only)."""
    parsed = parse_uuid(tenant_id)
    if parsed is None:
        return None
    found = await (await resolve(manager, IamAdminQueries)).tenant(parsed)
    if found is None:
        return None
    snapshot = snapshot_of(manager)
    if found[0].is_deleted:
        return found if snapshot.decide(TENANTS_MANAGE).allowed else None
    return found if in_scope(snapshot, parsed) else None


# --- texts ---------------------------------------------------------------------------------


def _error_messages() -> dict[str, str]:
    return {
        "permission_denied": _("Недостаточно прав для этого действия."),
        "privilege_escalation": _("Нельзя выдать права, которых нет у вас самих."),
        "self_modification_forbidden": _("Нельзя изменять самого себя."),
        "superuser_protected": _("Суперпользователя нельзя изменить."),
        "system_role_protected": _("Системную роль нельзя изменить."),
        "global_permission_tenant_override": _(
            "Глобальное право нельзя переопределить для магазина."
        ),
        "moderator_has_global_access": _(
            "Сотрудник с доступом ко всем магазинам не привязывается к магазинам."
        ),
        "expires_at_not_in_future": _("Срок действия должен быть в будущем."),
        "invalid_expires_at": _("Некорректный срок действия."),
        "invalid_name": _("Название не должно быть пустым."),
        "invalid_tenant_slug": _(
            "Некорректный slug: латиница в нижнем регистре, цифры и дефисы, до 64 символов."
        ),
        "invalid_telegram_user_id": _("Некорректный Telegram ID."),
        "invalid_settings": _("Некорректное значение настройки."),
        "tenant_slug_taken": _("Этот slug уже занят (в том числе удалённым магазином)."),
        "role_slug_taken": _("Этот slug роли уже занят."),
        "invalid_role_slug": _(
            "Некорректный slug роли: латиница в нижнем регистре, цифры и «_», начинается с буквы."
        ),
        "user_exists": _("Такой сотрудник уже есть."),
        "user_deleted": _("Сотрудник удалён — его можно восстановить."),
        "user_not_found": _("Сотрудник не найден."),
        "tenant_not_found": _("Магазин не найден."),
        "role_not_found": _("Роль не найдена."),
        "permission_not_found": _("Право не найдено."),
        "override_not_found": _("Персональное право не найдено."),
        "role_already_assigned": _("Роль уже назначена."),
        "role_not_assigned": _("Роль не назначена."),
        "moderator_already_assigned": _("Сотрудник уже привязан к этому магазину."),
        "moderator_not_found": _("Сотрудник не привязан к этому магазину."),
        "language_not_found": _("Язык не найден или отключён."),
        "conflict": _("Данные изменились параллельно, повторите действие."),
        "session_not_clean": _("Не удалось выполнить действие, повторите попытку."),
    }


def cache_warning() -> str:
    return _("Изменение сохранено, но кэш прав обновится с задержкой.")


register_error_texts(_error_messages)
register_stale_cache_texts({"permissions": cache_warning})


def mark(flag: bool) -> str:
    return "✅" if flag else "▫️"


def yes_no(flag: bool) -> str:
    return _("да") if flag else _("нет")


def status_text(*, is_active: bool, is_deleted: bool) -> str:
    if is_deleted:
        return _("удалён")
    return _("активен") if is_active else _("отключён")
