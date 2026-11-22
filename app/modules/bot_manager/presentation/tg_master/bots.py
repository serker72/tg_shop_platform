"""Tenant-scoped bot management dialog.

Every getter and callback re-authorizes through BotControlService; dialog state is not trust.
"""

# ruff: noqa: E501
from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import (
    Button,
    Cancel,
    CopyText,
    ListGroup,
    Row,
    Select,
    SwitchTo,
    Url,
)
from aiogram_dialog.widgets.text import Const, Format

from app.modules.bot_manager.presentation.tg_master.states import BotsSG
from app.modules.bot_manager.service import BotControlService
from app.shared_kernel.presentation.tg_master.common import actor_id, resolve

PAGE_SIZE = 8


def _tenant_id(manager: DialogManager) -> UUID:
    start_data = cast(dict[str, Any], manager.start_data)
    dialog_data = cast(dict[str, Any], manager.dialog_data)
    value = start_data.get("tenant_id") or dialog_data.get("tenant_id")
    return UUID(str(value))


async def _service(manager: DialogManager) -> BotControlService:
    return await resolve(manager, BotControlService)


async def list_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    service = await _service(dialog_manager)
    tenant_id = _tenant_id(dialog_manager)
    bots = await service.list_for_tenant(actor_id(dialog_manager), tenant_id)
    dialog_data = cast(dict[str, Any], dialog_manager.dialog_data)
    page = max(0, int(dialog_data.get("page", 0)))
    start = page * PAGE_SIZE
    rows = bots[start : start + PAGE_SIZE]
    labels = {
        "running": "🟢",
        "starting": "🟡",
        "stopped": "⚪",
        "invalid_token": "🔑",
        "error": "🔴",
    }
    items = [
        (
            str(b.id),
            f"{labels.get(b.status, '🔴')} @{b.username or b.name or b.telegram_bot_id} "
            f"{('✅' if b.is_active else '⏸')}",
            "⏹" if b.is_active else "▶️",
        )
        for b in rows
    ]
    return {
        "items": items,
        "has_prev": page > 0,
        "has_next": start + PAGE_SIZE < len(bots),
        "tenant_id": str(tenant_id),
        "text": "Боты",
    }


async def choose_bot(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: str
) -> None:
    service = await _service(manager)
    await service.list_for_tenant(actor_id(manager), _tenant_id(manager))
    manager.dialog_data["bot_id"] = item_id
    await manager.switch_to(BotsSG.card)


async def toggle_bot(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    """Toggle a bot after reloading the tenant-scoped row from the server.

    The ListGroup item id is untrusted callback data.  ``list_for_tenant`` checks
    PermissionService and tenant ownership; ``start``/``stop`` repeat the check and
    reject inactive or deleted tenants.
    """
    service = await _service(manager)
    bot_id = UUID(str(getattr(manager, "item_id", "")))
    bots = await service.list_for_tenant(actor_id(manager), _tenant_id(manager))
    bot = next((item for item in bots if item.id == bot_id), None)
    if bot is None:
        from app.shared_kernel.domain.exceptions import EntityNotFoundError

        raise EntityNotFoundError("Bot not found", code="bot_not_found")
    if bot.is_active:
        await service.stop(bot.id, actor_id(manager))
    else:
        await service.start(bot.id, actor_id(manager))
    await manager.switch_to(BotsSG.list)


async def page_prev(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _service(manager)
    manager.dialog_data["page"] = max(0, int(manager.dialog_data.get("page", 0)) - 1)
    await manager.switch_to(BotsSG.list)


async def page_next(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _service(manager)
    manager.dialog_data["page"] = int(manager.dialog_data.get("page", 0)) + 1
    await manager.switch_to(BotsSG.list)


async def card_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    service = await _service(dialog_manager)
    bot_id = UUID(str(dialog_manager.dialog_data["bot_id"]))
    bots = await service.list_for_tenant(actor_id(dialog_manager), _tenant_id(dialog_manager))
    bot = next((item for item in bots if item.id == bot_id), None)
    if bot is None:
        return {"text": "Бот не найден"}
    return {
        "text": f"{bot.name or ''} @{bot.username or ''}\nСтатус: {bot.status}\nАктивен: {'да' if bot.is_active else 'нет'}"
    }


async def start(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await (await _service(manager)).start(
        UUID(str(manager.dialog_data["bot_id"])), actor_id(manager)
    )
    await manager.switch_to(BotsSG.card)


async def stop(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await (await _service(manager)).stop(
        UUID(str(manager.dialog_data["bot_id"])), actor_id(manager)
    )
    await manager.switch_to(BotsSG.card)


async def restart(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await (await _service(manager)).restart(
        UUID(str(manager.dialog_data["bot_id"])), actor_id(manager)
    )
    await manager.switch_to(BotsSG.card)


async def deactivate(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await (await _service(manager)).deactivate(
        UUID(str(manager.dialog_data["bot_id"])), actor_id(manager)
    )
    await manager.switch_to(BotsSG.list)


async def token_received(message: Message, widget: Any, manager: DialogManager, data: str) -> None:
    try:
        await (await _service(manager)).add_bot(_tenant_id(manager), data, actor_id(manager))
    finally:
        await message.delete()
    await manager.switch_to(BotsSG.list)


async def replacement_received(
    message: Message, widget: Any, manager: DialogManager, data: str
) -> None:
    try:
        await (await _service(manager)).replace_token(
            UUID(str(manager.dialog_data["bot_id"])), data, actor_id(manager)
        )
    finally:
        await message.delete()
    await manager.switch_to(BotsSG.card)


def bots_dialog() -> Dialog:
    return Dialog(
        Window(
            Format("{text}"),
            ListGroup(
                Row(
                    Select(
                        Format("{item[1]}"),
                        id="bsel",
                        items="items",
                        item_id_getter=lambda x: x[0],
                        on_click=choose_bot,
                    ),
                    Button(Format("{item[2]}"), id="toggle", on_click=toggle_bot),
                ),
                id="bots",
                items="items",
                item_id_getter=lambda x: x[0],
            ),
            Row(
                Button(Const("‹"), id="prev", on_click=page_prev, when="has_prev"),
                Button(Const("›"), id="next", on_click=page_next, when="has_next"),
            ),
            SwitchTo(Const("➕ Добавить бота"), id="add", state=BotsSG.add_instruction),
            state=BotsSG.list,
            getter=list_getter,
        ),
        Window(
            Const("Создайте бота через BotFather, затем отправьте токен"),
            Url(Const("BotFather"), url=Const("https://t.me/BotFather")),
            CopyText(Const("Скопировать /newbot"), Const("/newbot")),
            SwitchTo(Const("Продолжить"), id="continue", state=BotsSG.add_token),
            Cancel(),
            state=BotsSG.add_instruction,
        ),
        Window(
            Const("Отправьте токен"),
            TextInput(id="token", on_success=token_received),
            state=BotsSG.add_token,
        ),
        Window(
            Format("{text}"),
            Button(Const("▶️ Запустить"), id="start", on_click=start),
            Button(Const("⏹ Остановить"), id="stop", on_click=stop),
            Button(Const("↻ Перезапустить"), id="restart", on_click=restart),
            SwitchTo(Const("Заменить токен"), id="replace", state=BotsSG.replace_token),
            Button(Const("Удалить"), id="delete", on_click=deactivate),
            SwitchTo(Const("Назад"), id="back", state=BotsSG.list),
            state=BotsSG.card,
            getter=card_getter,
        ),
        Window(
            Const("Отправьте новый токен"),
            TextInput(id="new_token", on_success=replacement_received),
            state=BotsSG.replace_token,
        ),
    )


__all__ = ["BotsSG", "bots_dialog", "list_getter", "card_getter"]
