"""Shared helpers of the Master Bot dialogs: DI lookup, actor, notices, error texts, mutations.

Texts that belong to a module are registered by that module when its dialog code is
imported (shared_kernel never imports ``app.modules.*``):

* :func:`register_error_texts` - a callable returning ``DomainError.code`` -> text; it is
  called on every lookup, so ``gettext`` uses the locale of the current update;
* :func:`register_stale_cache_texts` - cache kind -> callable returning the warning shown
  when a service reports that cache as not reset (:class:`StaleCacheReporting`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

import structlog
from aiogram_dialog import DialogManager
from dishka.integrations.aiogram import CONTAINER_NAME

from app.core.i18n import gettext as _
from app.shared_kernel.domain.exceptions import DomainError

__all__ = [
    "CONTAINER_NAME",
    "LABEL_LIMIT",
    "NOTICE_KEY",
    "ErrorTexts",
    "Outcome",
    "StaleCacheReporting",
    "actor_id",
    "add_notice",
    "compose",
    "denied_text",
    "error_text",
    "mutate",
    "nav_texts",
    "register_error_texts",
    "register_stale_cache_texts",
    "resolve",
    "set_notice",
    "short",
    "stale_cache_text",
]

NOTICE_KEY: Final = "notice"
#: Max length of a button caption (longer names are cut with "…").
LABEL_LIMIT: Final = 60

#: Provider of ``DomainError.code`` -> localized text (evaluated per lookup).
ErrorTexts = Callable[[], Mapping[str, str]]

_error_text_providers: list[ErrorTexts] = []
_stale_cache_texts: dict[str, Callable[[], str]] = {}
_log = structlog.get_logger(__name__)


@runtime_checkable
class StaleCacheReporting(Protocol):
    """A write service that tells which caches its last call failed to reset."""

    @property
    def stale_caches(self) -> tuple[str, ...]: ...


def register_error_texts(provider: ErrorTexts) -> None:
    """Add the error texts of a module (idempotent; later providers win on a clash)."""
    if provider not in _error_text_providers:
        _error_text_providers.append(provider)


def register_stale_cache_texts(texts: Mapping[str, Callable[[], str]]) -> None:
    """Warning text per cache kind of :attr:`StaleCacheReporting.stale_caches`."""
    _stale_cache_texts.update(texts)


def actor_id(manager: DialogManager) -> int:
    user_id: int = manager.middleware_data["event_from_user"].id
    return user_id


async def resolve[T](manager: DialogManager, kind: type[T]) -> T:
    """Dependency from the Dishka REQUEST container of the current update."""
    result: T = await manager.middleware_data[CONTAINER_NAME].get(kind)
    return result


# --- texts ---------------------------------------------------------------------------------


def error_text(code: str | None) -> str:
    """Localized text of a ``DomainError.code``; an unknown code -> generic text + log."""
    messages: dict[str, str] = {}
    for provider in _error_text_providers:
        messages.update(provider())
    if code in messages:
        return messages[code]
    if code is not None and code.startswith("invalid_"):
        return _("Некорректные данные.")
    _log.warning("master_bot_unknown_error_code", code=code)
    return _("Не удалось выполнить действие.")


def denied_text() -> str:
    return _("Нет доступа.")


def stale_cache_text(kind: str) -> str:
    """Warning of a not reset cache ``kind``; an unregistered kind -> generic text + log."""
    text = _stale_cache_texts.get(kind)
    if text is not None:
        return text()
    _log.warning("master_bot_unknown_stale_cache", kind=kind)
    return _("Изменение сохранено, но кэш обновится с задержкой.")


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
    # ``_noop`` is the write services' "requested state already held" flag.
    notice = _("Без изменений.") if getattr(service, "_noop", False) else _("Готово.")
    if not getattr(service, "cache_invalidated", True):
        stale = service.stale_caches if isinstance(service, StaleCacheReporting) else ()
        warnings = [stale_cache_text(kind) for kind in stale] or [stale_cache_text("")]
        notice = "\n".join([notice, *dict.fromkeys(warnings)])
    set_notice(manager, notice)
    return Outcome(ok=True, value=result)
