"""Shared helpers of the IAM dialogs: DI lookup, actor, notices, ``DomainError`` mapping."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

import structlog
from aiogram_dialog import DialogManager
from dishka.integrations.aiogram import CONTAINER_NAME

from app.core.i18n import gettext as _
from app.modules.iam.snapshot import PermissionSnapshot
from app.shared_kernel.domain.exceptions import DomainError

__all__ = [
    "CONTAINER_NAME",
    "actor_id",
    "cache_warning",
    "compose",
    "error_text",
    "mutate",
    "resolve",
    "set_notice",
    "snapshot_of",
]

NOTICE_KEY: Final = "notice"

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
        "expires_at_not_in_future": _("Срок действия должен быть в будущем."),
        "invalid_expires_at": _("Некорректный срок действия."),
        "tenant_slug_taken": _("Этот slug уже занят (в том числе удалённым магазином)."),
        "role_slug_taken": _("Этот slug роли уже занят."),
        "user_exists": _("Такой сотрудник уже есть."),
        "user_deleted": _("Сотрудник удалён — восстановите его из списка."),
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


def error_text(code: str | None) -> str:
    """Localized text of a ``DomainError.code``; an unknown code -> generic text + log."""
    messages = _error_messages()
    if code in messages:
        return messages[code]
    if code is not None and code.startswith("invalid_"):
        return _("Некорректные данные.")
    _log.warning("master_bot_unknown_error_code", code=code)
    return _("Не удалось выполнить действие.")


def cache_warning() -> str:
    return _("Изменение сохранено, но кэш прав обновится с задержкой.")


def set_notice(manager: DialogManager, text: str) -> None:
    manager.dialog_data[NOTICE_KEY] = text


def compose(manager: DialogManager, *lines: str) -> str:
    """Window text: the pending notice (shown once) followed by ``lines``."""
    notice = manager.dialog_data.pop(NOTICE_KEY, "") or ""
    body = "\n".join(line for line in lines if line)
    return f"{notice}\n\n{body}" if notice else body


async def mutate[S, T](
    manager: DialogManager, kind: type[S], call: Callable[[S], Awaitable[T]]
) -> tuple[bool, T | None]:
    """Run one service mutation; errors and a stale cache become a notice of the window."""
    service = await resolve(manager, kind)
    try:
        result = await call(service)
    except DomainError as error:
        _log.info("master_bot_action_rejected", code=error.code)
        set_notice(manager, error_text(error.code))
        return False, None
    set_notice(manager, "" if getattr(service, "cache_invalidated", True) else cache_warning())
    return True, result


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
