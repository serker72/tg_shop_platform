"""Shared helpers of the IAM dialogs: DI lookup, actor, read scope, notices, error mapping.

``when`` of aiogram-dialog widgets works only at render time: a forged ``callback_data``
still reaches hidden buttons and any ``Select`` item id. So every getter/handler re-checks
read access on the server (:func:`load_tenant`, :func:`can_view_staff`); mutations are
checked by the admin services themselves.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import structlog
from aiogram_dialog import DialogManager
from dishka.integrations.aiogram import CONTAINER_NAME

from app.core.i18n import gettext as _
from app.modules.iam.models import Tenant, TenantSettings
from app.modules.iam.permissions import TENANTS_MANAGE, USERS_VIEW
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.iam.snapshot import PermissionSnapshot
from app.shared_kernel.domain.exceptions import DomainError

__all__ = [
    "BIGINT_MAX",
    "CONTAINER_NAME",
    "LABEL_LIMIT",
    "Outcome",
    "actor_id",
    "add_notice",
    "can_view_staff",
    "compose",
    "denied_text",
    "error_text",
    "in_scope",
    "load_tenant",
    "mutate",
    "resolve",
    "set_notice",
    "short",
    "snapshot_of",
]

NOTICE_KEY: Final = "notice"
#: Telegram ids are stored in PostgreSQL ``bigint``.
BIGINT_MAX: Final = 2**63
#: Max length of a button caption (longer names are cut with "…").
LABEL_LIMIT: Final = 60
#: Global permissions that open the "Staff" section (list and cards of staff users);
#: the superuser passes every check of the snapshot.
STAFF_VIEW_PERMISSIONS: Final = (USERS_VIEW,)

_log = structlog.get_logger(__name__)


def snapshot_of(manager: DialogManager) -> PermissionSnapshot:
    from app.entrypoints.master_bot.middlewares import SNAPSHOT_KEY

    snapshot: PermissionSnapshot = manager.middleware_data[SNAPSHOT_KEY]
    return snapshot


def actor_id(manager: DialogManager) -> int:
    user_id: int = manager.middleware_data["event_from_user"].id
    return user_id


async def resolve[T](manager: DialogManager, kind: type[T]) -> T:
    """Dependency from the Dishka REQUEST container of the current update."""
    result: T = await manager.middleware_data[CONTAINER_NAME].get(kind)
    return result


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
        # system settings (task 07)
        "retention_guard": _(
            "Изменение отменено: конфигурация pg_partman для audit_logs небезопасна "
            "(архивирование партиций выключено). Пока она не исправлена, сроки хранения "
            "логов изменить нельзя. Обратитесь к администратору БД."
        ),
        "retention_config_error": _(
            "Изменение отменено: конфигурация pg_partman для таблиц логов не найдена "
            "или некорректна. Обратитесь к администратору БД."
        ),
        "settings_busy": _(
            "Настройки сейчас изменяются другим процессом, повторите через несколько секунд."
        ),
        "system_settings_missing": _(
            "Системные настройки не инициализированы. Обратитесь к администратору."
        ),
    }


def error_text(code: str | None) -> str:
    """Localized text of a ``DomainError.code``; an unknown code -> generic text + log."""
    messages = _error_messages()
    if code in messages:
        return messages[code]
    if code is not None and code.startswith("invalid_"):
        return _("Некорректные данные.")
    _log.warning("master_bot_unknown_error_code", code=code)
    return _("Не удалось выполнить действие.")


def denied_text() -> str:
    return _("Нет доступа.")


def cache_warning() -> str:
    return _("Изменение сохранено, но кэш прав обновится с задержкой.")


def short(text: str, limit: int = LABEL_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def set_notice(manager: DialogManager, text: str) -> None:
    manager.dialog_data[NOTICE_KEY] = text


def add_notice(manager: DialogManager, text: str) -> None:
    """Append a line to the pending notice (e.g. after the result of :func:`mutate`)."""
    current = manager.dialog_data.get(NOTICE_KEY) or ""
    manager.dialog_data[NOTICE_KEY] = f"{current}\n{text}" if current else text


def compose(manager: DialogManager, *lines: str) -> str:
    """Window text: the pending notice (shown once) followed by ``lines``."""
    notice = manager.dialog_data.pop(NOTICE_KEY, "") or ""
    body = "\n".join(line for line in lines if line)
    return f"{notice}\n\n{body}" if notice else body


# --- mutations -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome[T]:
    ok: bool
    value: T | None = None
    code: str | None = None


async def mutate[S, T](
    manager: DialogManager, kind: type[S], call: Callable[[S], Awaitable[T]]
) -> Outcome[T]:
    """Run one service mutation; the result (done / no change / error / stale cache)
    becomes a notice of the next rendered window."""
    service = await resolve(manager, kind)
    try:
        result = await call(service)
    except DomainError as error:
        _log.info("master_bot_action_rejected", code=error.code)
        set_notice(manager, error_text(error.code))
        return Outcome(ok=False, code=error.code)
    # ``_noop`` is the admin services' "requested state already held" flag.
    notice = _("Без изменений.") if getattr(service, "_noop", False) else _("Готово.")
    if not getattr(service, "cache_invalidated", True):
        notice = f"{notice}\n{cache_warning()}"
    set_notice(manager, notice)
    return Outcome(ok=True, value=result)


def mark(flag: bool) -> str:
    return "✅" if flag else "▫️"


def yes_no(flag: bool) -> str:
    return _("да") if flag else _("нет")


def status_text(*, is_active: bool, is_deleted: bool) -> str:
    if is_deleted:
        return _("удалён")
    return _("активен") if is_active else _("отключён")


def nav_texts() -> dict[str, Any]:
    return {
        "btn_back": _("« Назад"),
        "btn_yes": _("Да"),
        "btn_no": _("Нет"),
        "btn_activate": _("Активировать"),
        "btn_deactivate": _("Деактивировать"),
        "btn_delete": _("Удалить"),
        "btn_restore": _("Восстановить"),
    }
