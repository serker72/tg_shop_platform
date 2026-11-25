"""Global proxy-pool management in the Master Bot."""

import re
from datetime import UTC, datetime
from hashlib import sha256
from html import escape
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import structlog
from aiogram.exceptions import ClientDecodeError, TelegramAPIError
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import ManagedTextInput, MessageInput, TextInput
from aiogram_dialog.widgets.kbd import Button, Cancel, Column, ScrollingGroup, Select, SwitchTo
from aiogram_dialog.widgets.text import Format

from app.core.i18n import gettext as _
from app.core.proxy_url import PROXY_URL_SCHEMES
from app.modules.bot_manager.models import (
    PROXY_STATUS_ACTIVE,
    PROXY_STATUS_BANNED,
    PROXY_STATUS_QUARANTINE,
)
from app.modules.bot_manager.presentation.tg_master.states import ProxiesSG
from app.modules.bot_manager.proxy_service import (
    INVALID_PROXY_URL,
    PROXY_EXISTS,
    PROXY_NOT_FOUND,
    PROXY_STATUS_CHANGED,
    ProxyPoolService,
    ProxySummary,
    ProxyVersion,
)
from app.modules.iam.permissions import PROXIES_MANAGE
from app.modules.iam.snapshot import PermissionSnapshot
from app.shared_kernel.domain.exceptions import DomainError, PermissionDeniedError
from app.shared_kernel.presentation.tg_master.common import actor_id, compose, resolve, set_notice

_log = structlog.get_logger(__name__)


def _label(value: str) -> str:
    # TextInput calls this BEFORE storing the original. In particular, ':', '/', '@',
    # '%' and control characters are forbidden, not stripped from a pasted URL.
    if not 1 <= len(value) <= 60 or not all(c.isalnum() or c in " _-" for c in value):
        raise ValueError("Invalid proxy label")
    if not any(c.isalnum() for c in value):
        raise ValueError("Invalid proxy label")
    return value.strip()


class _CanEnterLabel(Filter):
    async def __call__(self, message: Message, **data: Any) -> bool:
        from app.entrypoints.master_bot.middlewares import SNAPSHOT_KEY

        snapshot: PermissionSnapshot | None = data.get(SNAPSHOT_KEY)
        return snapshot is not None and snapshot.decide(PROXIES_MANAGE).allowed


async def _service(manager: DialogManager) -> ProxyPoolService:
    return await resolve(manager, ProxyPoolService)


def _error(error: DomainError) -> str:
    if isinstance(error, PermissionDeniedError):
        return _("Нет доступа.")
    return {
        INVALID_PROXY_URL: _("Неверный адрес прокси. Допустимые схемы: socks5, socks4 и http."),
        PROXY_EXISTS: _("Такой прокси уже добавлен."),
        PROXY_NOT_FOUND: _("Прокси не найден."),
        PROXY_STATUS_CHANGED: _("Карточка прокси устарела. Повторите действие."),
    }.get(error.code or "", _("Не удалось выполнить действие."))


def _status(status: str) -> str:
    return {
        PROXY_STATUS_ACTIVE: _("Активен"),
        PROXY_STATUS_BANNED: _("Заблокирован"),
        PROXY_STATUS_QUARANTINE: _("На карантине"),
    }[status]


def _display_label(proxy: ProxySummary) -> str:
    try:
        return _label(proxy.label or "")
    except ValueError:
        # Don't echo unsafe legacy labels (which might have been supplied by another caller).
        return "—"


def _details(proxy: ProxySummary, *, html: bool) -> str:
    moment = proxy.last_ban_at
    return _(
        "{label} · {address} · {status} · штрафы: {strikes} · боты: {load}/{limit} · бан: {ban}"
    ).format(
        label=_display_label(proxy),
        address=escape(proxy.masked_url) if html else proxy.masked_url,
        status=_status(proxy.status),
        strikes=proxy.strike_count,
        load=proxy.bot_count,
        limit=proxy.bot_limit,
        ban=moment.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC") if moment else "—",
    )


async def _labels(**_kwargs: Any) -> dict[str, str]:
    return {"back": _("« Назад"), "add": _("➕ Добавить прокси"), "yes": _("Да"), "no": _("Нет")}


async def _list(dialog_manager: DialogManager, **_kwargs: Any) -> dict[str, Any]:
    manager = dialog_manager
    try:
        proxies = await (await _service(manager)).list_proxies(actor_id(manager))
    except DomainError as error:
        return {"text": compose(manager, _error(error)), "items": [], "can_add": False}
    return {
        "text": compose(manager, _("Прокси"), "" if proxies else _("Пул прокси пуст.")),
        "items": [(proxy.id.hex, _details(proxy, html=False)) for proxy in proxies],
        "can_add": True,
    }


async def _current(manager: DialogManager) -> ProxySummary:
    service = await _service(manager)
    return await service.get_proxy(UUID(hex=manager.dialog_data["proxy_id"]), actor_id(manager))


async def _choose(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: str
) -> None:
    try:
        proxy_id = UUID(hex=item_id)
        await (await _service(manager)).get_proxy(proxy_id, actor_id(manager))
    except ValueError:
        return
    except DomainError as error:
        await callback.answer(_error(error))
        return
    manager.dialog_data.pop("pending", None)
    manager.dialog_data["proxy_id"] = proxy_id.hex
    await manager.switch_to(ProxiesSG.card)


def _actions(proxy: ProxySummary) -> dict[str, str]:
    def key(action: str) -> str:
        value = f"{proxy.id.hex}:{proxy.status}:{proxy.version.updated_at.isoformat()}:{action}"
        return sha256(value.encode()).hexdigest()[:32]

    actions = {}
    if proxy.status != PROXY_STATUS_BANNED:
        actions[key("ban")] = "ban"
    if proxy.status != PROXY_STATUS_ACTIVE:
        actions[key("activate")] = "activate"
    return actions


async def _card(dialog_manager: DialogManager, **_kwargs: Any) -> dict[str, Any]:
    manager = dialog_manager
    try:
        proxy = await _current(manager)
    except DomainError as error:
        return {"text": compose(manager, _error(error)), "actions": []}
    return {
        "text": compose(manager, _details(proxy, html=True)),
        "actions": [
            (key, _("Заблокировать") if action == "ban" else _("Активировать"))
            for key, action in _actions(proxy).items()
        ],
    }


async def _arm(callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: str) -> None:
    try:
        proxy = await _current(manager)
    except DomainError as error:
        await callback.answer(_error(error))
        return
    action = _actions(proxy).get(item_id)
    if action is None:
        await callback.answer(_("Карточка прокси устарела. Повторите действие."))
        return
    manager.dialog_data["pending"] = {
        "token": uuid4().hex,
        "id": proxy.id.hex,
        "action": action,
        "status": proxy.status,
        "updated_at": proxy.version.updated_at.isoformat(),
    }
    await manager.switch_to(ProxiesSG.confirm)


async def _confirmation(dialog_manager: DialogManager, **_kwargs: Any) -> dict[str, Any]:
    manager = dialog_manager
    pending = manager.dialog_data.get("pending")
    try:
        proxy = await _current(manager)
    except DomainError as error:
        return {"text": compose(manager, _error(error)), "confirmation": []}
    if (
        not pending
        or pending["id"] != proxy.id.hex
        or pending["status"] != proxy.status
        or pending["updated_at"] != proxy.version.updated_at.isoformat()
    ):
        return {"text": _("Карточка прокси устарела. Повторите действие."), "confirmation": []}
    question = (
        _("Заблокировать прокси? Назначенные боты будут отвязаны.")
        if pending["action"] == "ban"
        else _("Вернуть прокси в активный пул?")
    )
    details = (
        f"{_display_label(proxy)} · {escape(proxy.masked_url)}"
        if pending["action"] == "ban"
        else _details(proxy, html=True)
    )
    return {
        "text": compose(manager, question, details),
        "confirmation": [(pending["token"], _("Да"))],
    }


async def _apply(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: str
) -> None:
    pending = manager.dialog_data.get("pending")
    if (
        not pending
        or pending["token"] != item_id
        or pending["id"] != manager.dialog_data.get("proxy_id")
        or pending["action"] not in {"ban", "activate"}
    ):
        await callback.answer(_("Карточка прокси устарела. Повторите действие."))
        return
    manager.dialog_data.pop("pending")
    service = await _service(manager)
    proxy_id = UUID(hex=pending["id"])
    expected = ProxyVersion(pending["status"], datetime.fromisoformat(pending["updated_at"]))
    try:
        action = service.ban if pending["action"] == "ban" else service.activate
        await action(proxy_id, actor_id(manager), expected=expected)
    except DomainError as error:
        set_notice(manager, _error(error))
    else:
        set_notice(manager, _("Статус прокси обновлён."))
    await manager.switch_to(ProxiesSG.card)


async def _add(callback: CallbackQuery, widget: Button, manager: DialogManager) -> None:
    try:
        await (await _service(manager)).require_manage(actor_id(manager))
    except DomainError as error:
        await callback.answer(_error(error))
        return
    manager.dialog_data.pop("label", None)
    await manager.switch_to(ProxiesSG.add_label)


async def _prompt(dialog_manager: DialogManager, **_kwargs: Any) -> dict[str, str]:
    manager = dialog_manager
    try:
        await (await _service(manager)).require_manage(actor_id(manager))
    except DomainError as error:
        return {"text": compose(manager, _error(error))}
    if manager.current_context().state == ProxiesSG.add_label:
        text = _(
            "Введите название прокси (до 60 символов: буквы, цифры, пробел, дефис, подчёркивание)."
        )
    else:
        text = _("Отправьте URL прокси. Сообщение будет удалено перед сохранением.")
    return {"text": compose(manager, text)}


async def _save_label(
    message: Message, widget: ManagedTextInput[str], manager: DialogManager, value: str
) -> None:
    try:
        await (await _service(manager)).require_manage(actor_id(manager))
    except DomainError as error:
        set_notice(manager, _error(error))
        return
    manager.dialog_data["label"] = value
    await manager.switch_to(ProxiesSG.add_url)


def _looks_like_proxy_or_credential_url(text: str) -> bool:
    try:
        parts = urlsplit(text)
        return bool(
            parts.hostname
            and (
                parts.scheme in PROXY_URL_SCHEMES
                or parts.username is not None
                or parts.password is not None
            )
        )
    except ValueError:
        # Recognize a malformed URL for deletion only, not for saving. Limit the
        # credential marker to the authority so ordinary labels are not deleted.
        # Like urlsplit, discard embedded TAB, CR and LF before recognizing the URL.
        normalized = text.replace("\t", "").replace("\r", "").replace("\n", "")
        match = re.match(
            r"(?:(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*):)?//(?P<authority>[^/?#\s]+)",
            normalized.strip(),
        )
        if not match:
            return False
        if (match["scheme"] or "").lower() in PROXY_URL_SCHEMES:
            return True
        userinfo, separator, host = match["authority"].rpartition("@")
        return bool(separator and userinfo and host)


async def _bad_label(
    message: Message, widget: ManagedTextInput[str], manager: DialogManager, error: ValueError
) -> None:
    notice = _("Некорректное название прокси. Не вводите здесь URL.")
    if _looks_like_proxy_or_credential_url(message.text or ""):
        try:
            deleted = await message.delete()
        except Exception:
            # Do not propagate exceptions that could contain the rejected input.
            deleted = False
        if not deleted:
            notice += "\n" + _("Не удалось удалить сообщение. Удалите его вручную.")
    set_notice(manager, notice)


async def _receive_url(message: Message, widget: MessageInput, manager: DialogManager) -> None:
    try:
        if not await message.delete():
            raise ValueError("Message not deleted")
    except (ClientDecodeError, TelegramAPIError, ValueError):
        set_notice(
            manager, _("Не удалось удалить сообщение. Удалите его вручную и повторите ввод.")
        )
        return
    try:
        # Do not cross the secret boundary with an exception or the original URL.
        service = await _service(manager)
        proxy_id = await service.add(
            message.text or "", _label(manager.dialog_data["label"]), actor_id(manager)
        )
    except DomainError as error:
        set_notice(manager, _error(error))
        return
    except Exception:
        _log.warning("proxy_url_input_failed")
        set_notice(manager, _("Не удалось выполнить действие."))
        return
    manager.dialog_data.pop("label", None)
    manager.dialog_data["proxy_id"] = proxy_id.hex
    await manager.switch_to(ProxiesSG.card)


def proxies_dialog() -> Dialog:
    return Dialog(
        Window(
            Format("{text}"),
            ScrollingGroup(
                Column(
                    Select(
                        Format("{item[1]}"),
                        id="px",
                        item_id_getter=lambda item: item[0],
                        items="items",
                        on_click=_choose,
                    )
                ),
                id="pool",
                height=6,
                hide_on_single_page=True,
            ),
            Button(Format("{add}"), id="add", on_click=_add, when="can_add"),
            Cancel(Format("{back}")),
            state=ProxiesSG.list,
            getter=_list,
        ),
        Window(
            Format("{text}"),
            Column(
                Select(
                    Format("{item[1]}"),
                    id="pa",
                    item_id_getter=lambda item: item[0],
                    items="actions",
                    on_click=_arm,
                )
            ),
            SwitchTo(Format("{back}"), id="list", state=ProxiesSG.list),
            state=ProxiesSG.card,
            getter=_card,
        ),
        Window(
            Format("{text}"),
            TextInput(
                "label",
                type_factory=_label,
                on_success=_save_label,
                on_error=_bad_label,
                filter=_CanEnterLabel(),
            ),
            SwitchTo(Format("{back}"), id="list", state=ProxiesSG.list),
            state=ProxiesSG.add_label,
            getter=_prompt,
        ),
        Window(
            Format("{text}"),
            MessageInput(_receive_url),
            SwitchTo(Format("{back}"), id="label_back", state=ProxiesSG.add_label),
            state=ProxiesSG.add_url,
            getter=_prompt,
        ),
        Window(
            Format("{text}"),
            Select(
                Format("{item[1]}"),
                id="pc",
                item_id_getter=lambda item: item[0],
                items="confirmation",
                on_click=_apply,
            ),
            SwitchTo(Format("{no}"), id="card", state=ProxiesSG.card),
            state=ProxiesSG.confirm,
            getter=_confirmation,
        ),
        getter=_labels,
    )
