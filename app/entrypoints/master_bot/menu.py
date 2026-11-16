"""Main menu skeleton (aiogram-dialog): section buttons visible by permissions.

"Shops", "Staff" (task 05) and "Roles" (task 06) open the IAM dialogs; the other sections
are built in tasks 07-09 and their buttons only answer "in progress".
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
    LOGS_VIEW,
    PROXIES_MANAGE,
    ROLES_MANAGE,
    SETTINGS_MANAGE,
    TENANTS_VIEW_ALL,
)
from app.modules.iam.presentation.tg_master import (
    RolesSG,
    StaffSG,
    TenantsSG,
    roles_dialog,
    staff_dialog,
    tenants_dialog,
)
from app.modules.iam.presentation.tg_master.common import STAFF_VIEW_PERMISSIONS
from app.modules.iam.snapshot import PermissionSnapshot

__all__ = ["MENU_PERMISSIONS", "MainMenu", "build_menu_router", "main_menu_getter"]

#: Section -> permissions any of which shows its button (shops/bots: also own shops).
SECTION_PERMISSIONS: Final[dict[str, tuple[str, ...]]] = {
    "shops": (TENANTS_VIEW_ALL,),
    # ``users.view``: single source with the server-side check of the staff dialog.
    "staff": STAFF_VIEW_PERMISSIONS,
    "roles": (ROLES_MANAGE,),
    "settings": (SETTINGS_MANAGE,),
    "logs": (LOGS_VIEW,),
    "bots": (TENANTS_VIEW_ALL, PROXIES_MANAGE),
}
MENU_PERMISSIONS: Final = frozenset(s for slugs in SECTION_PERMISSIONS.values() for s in slugs)
_SHOP_SECTIONS: Final = frozenset({"shops", "bots"})
#: Sections that already have a dialog (the rest answer "in progress").
_SECTION_DIALOGS: Final[dict[str, State]] = {
    "shops": TenantsSG.list,
    "staff": StaffSG.list,
    "roles": RolesSG.list,
}


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


async def _open_section(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    """Re-check the section rights: ``when`` hides a button but a forged callback still
    reaches this handler."""
    from app.entrypoints.master_bot.middlewares import SNAPSHOT_KEY

    section = str(button.widget_id)
    if not visible_sections(manager.middleware_data[SNAPSHOT_KEY]).get(section, False):
        await callback.answer(Texts.no_access())
        return
    await manager.start(_SECTION_DIALOGS[section])


def _button(section: str) -> Button:
    handler = _open_section if section in _SECTION_DIALOGS else _section_soon
    return Button(Format(f"{{btn_{section}}}"), id=section, on_click=handler, when=f"can_{section}")


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
    router.include_router(tenants_dialog())
    router.include_router(staff_dialog())
    router.include_router(roles_dialog())
    return router
