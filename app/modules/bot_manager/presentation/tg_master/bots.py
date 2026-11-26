"""Tenant-scoped bot management dialog."""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import structlog
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ContentType, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import (
    Button,
    Cancel,
    CopyText,
    ListGroup,
    Row,
    SwitchTo,
    Url,
)
from aiogram_dialog.widgets.text import Const, Format

from app.core.i18n import gettext as _
from app.modules.bot_manager.models import Bot
from app.modules.bot_manager.presentation.tg_master.states import BotsSG
from app.modules.bot_manager.service import BotControlService
from app.modules.iam.presentation.tg_master.common import load_tenant
from app.shared_kernel.domain.exceptions import DomainError, EntityNotFoundError
from app.shared_kernel.presentation.tg_master.common import actor_id, compose, resolve, set_notice

ICONS = {"running": "🟢", "starting": "🟡", "stopped": "⚪", "invalid_token": "🔑", "error": "🔴"}


def _uuid(value: object) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise EntityNotFoundError("Not found", code="bot_not_found") from None


def _tenant(manager: DialogManager) -> UUID:
    data = manager.start_data
    return _uuid(data.get("tenant_id") if isinstance(data, dict) else None)


async def _service(manager: DialogManager) -> BotControlService:
    if await load_tenant(manager, _tenant(manager)) is None:
        raise EntityNotFoundError("Tenant not found", code="tenant_not_found")
    return await resolve(manager, BotControlService)


def _error(error: DomainError) -> str:
    return {
        "invalid_token_format": _("Неверный формат токена."),
        "invalid_token": _("Telegram отклонил токен."),
        "bot_identity_mismatch": _("Токен принадлежит другому боту."),
        "bot_already_exists": _("Бот уже добавлен."),
        "bot_in_deleted_tenant": _("Бот уже добавлен и привязан к удалённому магазину."),
        "no_proxy_available": _("Нет свободного прокси."),
        "telegram_unavailable": _("Telegram недоступен. Попробуйте ещё раз."),
        "tenant_inactive": _("Магазин доступен только для просмотра."),
    }.get(error.code if error.code is not None else "", _("Операция недоступна."))


def _label(bot: Any) -> str:
    return " ".join(filter(None, (bot.name, f"@{bot.username}" if bot.username else None))) or str(
        bot.telegram_bot_id
    )


def _tenant_stop_reason(error: str | None) -> str | None:
    if error == "tenant_inactive":
        return _("Магазин деактивирован.")
    if error == "tenant_deleted":
        return _("Магазин удалён.")
    return None


def _list_label(bot: Bot, *, read_only: bool) -> str:
    label = f"{ICONS[bot.status]} {_label(bot)} · " + (_("вкл") if bot.is_active else _("выкл"))
    reason = _tenant_stop_reason(bot.last_error) if read_only else None
    return f"{label} · {reason}" if reason else label


async def list_getter(dialog_manager: DialogManager, **_kwargs: Any) -> dict[str, Any]:
    manager = dialog_manager
    try:
        service, actor, tenant = await _service(manager), actor_id(manager), _tenant(manager)
        page = await service.page_for_tenant(actor, tenant, page=manager.dialog_data.get("page", 0))
    except DomainError as error:
        manager.dialog_data["page"] = 0
        return {"text": compose(manager, _error(error)), "items": [], "can_mutate": False}
    manager.dialog_data["page"] = page.index
    items = [
        (
            bot.id.hex,
            _list_label(bot, read_only=page.read_only),
            bot.is_active,
        )
        for bot in page.items
    ]
    title = _("Боты (только просмотр)") if page.read_only else _("Боты")
    return {
        "text": compose(manager, title),
        "items": items,
        "can_mutate": not page.read_only,
        "has_prev": page.index > 0,
        "has_next": page.index + 1 < page.page_count,
    }


def _change_page(step: int) -> Callable[..., Awaitable[None]]:
    async def handler(callback: CallbackQuery, widget: Any, manager: DialogManager) -> None:
        # The normal render runs the scoped getter once and clamps this relative request.
        manager.dialog_data["page"] = max(0, manager.dialog_data.get("page", 0) + step)

    return handler


async def _find(manager: DialogManager, bot_id: UUID) -> Bot:
    service = await _service(manager)
    return await service.get_for_tenant(actor_id(manager), _tenant(manager), bot_id)


async def choose_bot(callback: CallbackQuery, widget: Any, manager: DialogManager) -> None:
    try:
        bot = await _find(manager, _uuid(getattr(manager, "item_id", None)))
    except DomainError as error:
        await callback.answer(_error(error))
        return
    manager.dialog_data.pop("pending_bot", None)
    manager.dialog_data["bot_id"] = bot.id.hex
    await manager.switch_to(BotsSG.card)


def _row_action(*, start: bool) -> Callable[..., Awaitable[None]]:
    async def handler(callback: CallbackQuery, widget: Any, manager: DialogManager) -> None:
        try:
            bot_id = _uuid(getattr(manager, "item_id", None))
            await _find(manager, bot_id)
            service = await _service(manager)
            action = service.start if start else service.stop
            await action(bot_id, actor_id(manager), tenant_id=_tenant(manager))
        except DomainError as error:
            await callback.answer(_error(error))
            return
        await manager.switch_to(BotsSG.list)

    return handler


async def card_getter(dialog_manager: DialogManager, **_kwargs: Any) -> dict[str, Any]:
    manager = dialog_manager
    try:
        service = await _service(manager)
        read_only = await service.tenant_is_read_only(actor_id(manager), _tenant(manager))
        bot = await _find(manager, _uuid(manager.dialog_data.get("bot_id")))
        proxy = await service.proxy_display(bot)
    except DomainError as error:
        return {
            "text": compose(manager, _error(error)),
            "can_mutate": False,
            "card_items": [],
            "can_start": False,
            "can_stop": False,
        }
    return {
        "text": compose(
            manager,
            _label(bot),
            f"Telegram ID: {bot.telegram_bot_id}",
            f"{ICONS[bot.status]} {bot.status} · " + (_("вкл") if bot.is_active else _("выкл")),
            _("Ошибка: {error}").format(
                error=_tenant_stop_reason(bot.last_error) or bot.last_error or "—"
            ),
            _("Прокси: {proxy}").format(proxy=proxy or "—"),
        ),
        "card_items": [bot.id.hex],
        "can_mutate": not read_only,
        "can_start": not read_only and not bot.is_active,
        "can_stop": not read_only and bot.is_active,
    }


def _action(method: str, target: Any) -> Callable[..., Awaitable[None]]:
    async def handler(callback: CallbackQuery, widget: Any, manager: DialogManager) -> None:
        try:
            service = await _service(manager)
            await getattr(service, method)(
                _card_id(manager, pending=method == "deactivate"),
                actor_id(manager),
                tenant_id=_tenant(manager),
            )
        except DomainError as error:
            await callback.answer(_error(error))
            return
        await manager.switch_to(target)

    return handler


def _card_id(manager: DialogManager, *, pending: bool = False) -> UUID:
    value = getattr(manager, "item_id", None)
    if value != manager.dialog_data.get("bot_id") or (
        pending and value != manager.dialog_data.get("pending_bot")
    ):
        raise EntityNotFoundError("Stale bot card", code="bot_not_found")
    return _uuid(value)


async def _writable(manager: DialogManager) -> None:
    if await (await _service(manager)).tenant_is_read_only(actor_id(manager), _tenant(manager)):
        raise DomainError("Tenant is read-only", code="tenant_inactive")


def _enter(target: Any, *, card: bool = False) -> Callable[..., Awaitable[None]]:
    async def handler(callback: CallbackQuery, widget: Any, manager: DialogManager) -> None:
        try:
            await _writable(manager)
            if card:
                bot_id = _card_id(manager)
                await _find(manager, bot_id)
                manager.dialog_data["pending_bot"] = bot_id.hex
        except DomainError as error:
            await callback.answer(_error(error))
            return
        await manager.switch_to(target)

    return handler


async def _prompt(manager: DialogManager) -> dict[str, Any]:
    try:
        allowed = not await (await _service(manager)).tenant_is_read_only(
            actor_id(manager), _tenant(manager)
        )
    except DomainError as error:
        return {"notice": compose(manager, _error(error)), "can_mutate": False, "card_items": []}
    return {
        "notice": compose(manager, "" if allowed else _("Магазин доступен только для просмотра.")),
        "can_mutate": allowed,
        "card_items": [manager.dialog_data["pending_bot"]]
        if allowed and manager.dialog_data.get("pending_bot")
        else [],
    }


async def prompt_getter(dialog_manager: DialogManager, **_kwargs: Any) -> dict[str, Any]:
    return await _prompt(dialog_manager)


async def _submit_token(message: Message, manager: DialogManager, *, replace: bool) -> None:
    try:
        # Delete before network or database I/O; do not persist a token we could not remove.
        await message.delete()
    except TelegramAPIError:
        set_notice(
            manager, _("Не удалось удалить сообщение. Удалите его вручную и повторите ввод.")
        )
        return
    try:
        service, actor, tenant = await _service(manager), actor_id(manager), _tenant(manager)
        await _writable(manager)
        token = message.text or ""
        if replace:
            bound = manager.dialog_data.get("pending_bot")
            if bound != manager.dialog_data.get("bot_id"):
                raise EntityNotFoundError("Stale bot card", code="bot_not_found")
            bot = await service.replace_token(_uuid(bound), token, actor, tenant_id=tenant)
        else:
            bot = await service.add_bot(tenant, token, actor)
    except DomainError as error:
        set_notice(manager, _error(error))
        return
    manager.dialog_data.pop("pending_bot", None)
    manager.dialog_data["bot_id"] = bot.id.hex
    await manager.switch_to(BotsSG.card)


async def _token_received(message: Message, manager: DialogManager, *, replace: bool) -> None:
    # Never let the original exception or traceback cross the secret input boundary.
    try:
        await _submit_token(message, manager, replace=replace)
    except Exception:
        structlog.get_logger(__name__).warning("bot_token_input_failed")
        set_notice(manager, _("Операция недоступна."))


async def token_received(message: Message, widget: MessageInput, manager: DialogManager) -> None:
    await _token_received(message, manager, replace=False)


async def replacement_received(
    message: Message, widget: MessageInput, manager: DialogManager
) -> None:
    await _token_received(message, manager, replace=True)


async def labels(**_kwargs: Any) -> dict[str, str]:
    return {
        "add": _("➕ Добавить бота"),
        "back": _("Назад"),
        "copy": _("Скопировать /newbot"),
        "enter": _("Ввести токен"),
        "token": _("Отправьте токен"),
        "new_token": _("Отправьте новый токен"),
        "instruction": _(
            "1. Откройте @BotFather.\n2. Отправьте /newbot.\n"
            "3. Укажите имя и username, оканчивающийся на bot.\n"
            "4. Скопируйте токен. Нажмите «Ввести токен» и отправьте его "
            "на следующем экране."
        ),
        "start": _("▶️ Запустить"),
        "stop": _("⏹ Остановить"),
        "restart": _("🔄 Перезапустить"),
        "replace": _("🔑 Заменить токен"),
        "delete": _("🗑 Удалить"),
        "confirm": _("Удалить бота?"),
        "yes": _("Да, удалить"),
        "no": _("Нет"),
    }


def bots_dialog() -> Dialog:
    back = Format("{back}")
    return Dialog(
        Window(
            Format("{text}"),
            ListGroup(
                Row(
                    Button(Format("{item[1]}"), id="b", on_click=choose_bot),
                    Button(
                        Const("▶️"),
                        id="bt_start",
                        on_click=_row_action(start=True),
                        when=lambda data, *_: data["data"]["can_mutate"] and not data["item"][2],
                    ),
                    Button(
                        Const("⏹"),
                        id="bt_stop",
                        on_click=_row_action(start=False),
                        when=lambda data, *_: data["data"]["can_mutate"] and data["item"][2],
                    ),
                ),
                id="pg",
                items="items",
                item_id_getter=lambda x: x[0],
                page_size=0,
            ),
            Row(
                Button(Const("<"), id="prev", on_click=_change_page(-1), when="has_prev"),
                Button(Const(">"), id="next", on_click=_change_page(1), when="has_next"),
            ),
            Button(
                Format("{add}"),
                id="add",
                on_click=_enter(BotsSG.add_instruction),
                when="can_mutate",
            ),
            Cancel(back),
            state=BotsSG.list,
            getter=list_getter,
        ),
        Window(
            Format("{notice}{instruction}", when="can_mutate"),
            Format("{notice}", when=lambda data, *_: not data["can_mutate"]),
            Url(Format("BotFather"), url=Format("https://t.me/BotFather"), when="can_mutate"),
            CopyText(Format("{copy}"), Format("/newbot"), when="can_mutate"),
            Button(
                Format("{enter}"), id="enter", on_click=_enter(BotsSG.add_token), when="can_mutate"
            ),
            SwitchTo(back, id="back", state=BotsSG.list),
            state=BotsSG.add_instruction,
            getter=prompt_getter,
        ),
        Window(
            Format("{notice}"),
            Format("{token}", when="can_mutate"),
            MessageInput(token_received, content_types=ContentType.TEXT),
            SwitchTo(back, id="back", state=BotsSG.list),
            state=BotsSG.add_token,
            getter=prompt_getter,
        ),
        Window(
            Format("{text}"),
            ListGroup(
                Button(
                    Format("{data[start]}"),
                    id="start",
                    on_click=_action("start", BotsSG.card),
                    when=lambda data, *_: data["data"]["can_start"],
                ),
                Button(
                    Format("{data[stop]}"),
                    id="stop",
                    on_click=_action("stop", BotsSG.card),
                    when=lambda data, *_: data["data"]["can_stop"],
                ),
                Button(
                    Format("{data[restart]}"),
                    id="restart",
                    on_click=_action("restart", BotsSG.card),
                    when=lambda data, *_: data["data"]["can_mutate"],
                ),
                Button(
                    Format("{data[replace]}"),
                    id="replace",
                    on_click=_enter(BotsSG.replace_token, card=True),
                    when=lambda data, *_: data["data"]["can_mutate"],
                ),
                Button(
                    Format("{data[delete]}"),
                    id="delete",
                    on_click=_enter(BotsSG.confirm, card=True),
                    when=lambda data, *_: data["data"]["can_mutate"],
                ),
                id="card",
                items="card_items",
                item_id_getter=str,
            ),
            SwitchTo(back, id="back", state=BotsSG.list),
            state=BotsSG.card,
            getter=card_getter,
        ),
        Window(
            Format("{notice}{confirm}"),
            ListGroup(
                Button(
                    Format("{data[yes]}"), id="yes", on_click=_action("deactivate", BotsSG.list)
                ),
                id="confirm",
                items="card_items",
                item_id_getter=str,
            ),
            SwitchTo(Format("{no}"), id="no", state=BotsSG.card),
            state=BotsSG.confirm,
            getter=prompt_getter,
        ),
        Window(
            Format("{notice}"),
            Format("{new_token}", when="can_mutate"),
            MessageInput(replacement_received, content_types=ContentType.TEXT),
            SwitchTo(back, id="back", state=BotsSG.card),
            state=BotsSG.replace_token,
            getter=prompt_getter,
        ),
        getter=labels,
    )
