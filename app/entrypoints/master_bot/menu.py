"""Main menu skeleton (aiogram-dialog): section buttons visible by permissions.

Section dialogs are built in tasks 05-09; here a button only answers "in progress".
"""

from __future__ import annotations

from typing import Any, Final

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, StartMode, Window
from aiogram_dialog.widgets.kbd import Button, Column
from aiogram_dialog.widgets.text import Format

from app.entrypoints.master_bot.texts import Texts
from app.modules.iam.permissions import (
    MODERATORS_MANAGE,
    PROXIES_MANAGE,
    ROLES_MANAGE,
    SETTINGS_MANAGE,
    TENANTS_VIEW_ALL,
    USERS_MANAGE,
    USERS_VIEW,
)
from app.modules.iam.snapshot import PermissionSnapshot

__all__ = ["MENU_PERMISSIONS", "MainMenu", "build_menu_router", "main_menu_getter"]

#: Section -> permissions any of which shows its button (shops/bots: also own shops).
SECTION_PERMISSIONS: Final[dict[str, tuple[str, ...]]] = {
    "shops": (TENANTS_VIEW_ALL,),
    "staff": (USERS_VIEW, USERS_MANAGE, MODERATORS_MANAGE),
    "roles": (ROLES_MANAGE,),
    "settings": (SETTINGS_MANAGE,),
    "logs": (USERS_VIEW,),
    "bots": (TENANTS_VIEW_ALL, PROXIES_MANAGE),
}
MENU_PERMISSIONS: Final = frozenset(s for slugs in SECTION_PERMISSIONS.values() for s in slugs)
_SHOP_SECTIONS: Final = frozenset({"shops", "bots"})


class MainMenu(StatesGroup):
    main = State()


def visible_sections(snapshot: PermissionSnapshot) -> dict[str, bool]:
    result = {}
    for section, slugs in SECTION_PERMISSIONS.items():
        allowed = any(snapshot.decide(slug).allowed for slug in slugs)
        if section in _SHOP_SECTIONS and snapshot.tenants:
            allowed = True
        result[section] = allowed
    return result


async def main_menu_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    from app.entrypoints.master_bot.middlewares import SNAPSHOT_KEY

    snapshot: PermissionSnapshot = dialog_manager.middleware_data[SNAPSHOT_KEY]
    data: dict[str, Any] = {f"can_{k}": v for k, v in visible_sections(snapshot).items()}
    data.update(
        title=Texts.main_menu_title(),
        btn_shops=Texts.btn_shops(),
        btn_staff=Texts.btn_staff(),
        btn_roles=Texts.btn_roles(),
        btn_settings=Texts.btn_settings(),
        btn_logs=Texts.btn_logs(),
        btn_bots=Texts.btn_bots(),
    )
    return data


async def _section_soon(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await callback.answer(Texts.section_soon())


def _button(section: str) -> Button:
    return Button(
        Format(f"{{btn_{section}}}"), id=section, on_click=_section_soon, when=f"can_{section}"
    )


def build_main_menu_dialog() -> Dialog:
    return Dialog(
        Window(
            Format("{title}"),
            Column(*(_button(section) for section in SECTION_PERMISSIONS)),
            state=MainMenu.main,
            getter=main_menu_getter,
        )
    )


async def _start(message: Message, dialog_manager: DialogManager) -> None:
    await dialog_manager.start(MainMenu.main, mode=StartMode.RESET_STACK)


def build_menu_router() -> Router:
    router = Router(name="master_bot.menu")
    router.message.register(_start, CommandStart())
    router.include_router(build_main_menu_dialog())
    return router
