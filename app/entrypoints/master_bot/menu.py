"""Main menu skeleton (aiogram-dialog): section buttons visible by permissions.

"Shops", "Staff" (task 05) and "Roles" (task 06) open the IAM dialogs, "Settings" (task 07)
the system settings dialog; the other sections are built in tasks 08-09 and their buttons
only answer "in progress".
"""

from __future__ import annotations

from typing import Any, Final

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, StartMode, Window
from aiogram_dialog.api.internal import CONTEXT_KEY
from aiogram_dialog.widgets.kbd import Button, Column
from aiogram_dialog.widgets.text import Format

from app.core.i18n import gettext as _
from app.entrypoints.master_bot.texts import Texts
from app.modules.bot_manager.presentation.tg_master import bots_dialog
from app.modules.bot_manager.presentation.tg_master.states import BotsSG
from app.modules.iam.permissions import (
    LOGS_VIEW,
    ROLES_MANAGE,
    SETTINGS_MANAGE,
    TENANTS_VIEW_ALL,
)
from app.modules.iam.presentation.tg_master import (
    RolesSG,
    StaffSG,
    TenantsSG,
    effective_dialog,
    overrides_dialog,
    roles_dialog,
    staff_dialog,
    tenants_dialog,
)
from app.modules.iam.presentation.tg_master.common import STAFF_VIEW_PERMISSIONS
from app.modules.iam.presentation.tg_master.staff import open_self_timezone, self_timezone_user
from app.modules.iam.snapshot import PermissionSnapshot
from app.modules.logging.presentation.tg_master import LogsSG, logs_dialog
from app.modules.system.presentation.tg_master import SettingsSG, settings_dialog

__all__ = ["MENU_PERMISSIONS", "MainMenu", "build_menu_router", "main_menu_getter"]

#: Section -> permissions any of which shows its button (shops/bots: also own shops).
SECTION_PERMISSIONS: Final[dict[str, tuple[str, ...]]] = {
    "shops": (TENANTS_VIEW_ALL,),
    # ``users.view``: single source with the server-side check of the staff dialog.
    "staff": STAFF_VIEW_PERMISSIONS,
    "roles": (ROLES_MANAGE,),
    "settings": (SETTINGS_MANAGE,),
    "logs": (LOGS_VIEW,),
}
MENU_PERMISSIONS: Final = frozenset(s for slugs in SECTION_PERMISSIONS.values() for s in slugs)
_SHOP_SECTIONS: Final = frozenset({"shops"})
#: Sections that already have a dialog (the rest answer "in progress").
_SECTION_DIALOGS: Final[dict[str, State]] = {
    "shops": TenantsSG.list,
    "staff": StaffSG.list,
    "roles": RolesSG.list,
    "settings": SettingsSG.main,
    "logs": LogsSG.list,
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


async def main_menu_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    from app.entrypoints.master_bot.middlewares import SNAPSHOT_KEY, has_panel_access

    snapshot: PermissionSnapshot = dialog_manager.middleware_data[SNAPSHOT_KEY]
    data: dict[str, Any] = {f"can_{k}": v for k, v in visible_sections(snapshot).items()}
    data.update(
        title=Texts.main_menu_title(),
        btn_shops=Texts.btn_shops(),
        btn_staff=Texts.btn_staff(),
        btn_roles=Texts.btn_roles(),
        btn_settings=Texts.btn_settings(),
        btn_logs=Texts.btn_logs(),
        btn_timezone=_("Мой часовой пояс"),
        can_timezone=has_panel_access(snapshot),
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
            Column(
                *(_button(section) for section in SECTION_PERMISSIONS),
                Button(
                    Format("{btn_timezone}"),
                    id="my_tz",
                    on_click=open_self_timezone,
                    when="can_timezone",
                ),
            ),
            state=MainMenu.main,
            getter=main_menu_getter,
        )
    )


async def _start(message: Message, dialog_manager: DialogManager) -> None:
    """Offer staff onboarding on each /start until a timezone is saved."""
    user = await self_timezone_user(dialog_manager)
    if user is not None and getattr(user, "timezone", None) is None:
        await dialog_manager.start(StaffSG.onboarding, mode=StartMode.RESET_STACK)
        return
    await dialog_manager.start(MainMenu.main, mode=StartMode.RESET_STACK)


async def _outside_token_input(message: Message, **data: Any) -> bool:
    # The intent middleware loads the context before filters run; the manager is
    # injected later, and aiogram's raw FSM state is not the dialog state.
    context = data.get(CONTEXT_KEY)
    return context is None or context.state not in (BotsSG.add_token, BotsSG.replace_token)


def build_menu_router() -> Router:
    router = Router(name="master_bot.menu")
    router.message.register(_start, CommandStart(), _outside_token_input)
    router.include_router(build_main_menu_dialog())
    router.include_router(tenants_dialog())
    router.include_router(staff_dialog())
    router.include_router(roles_dialog())
    router.include_router(overrides_dialog())
    router.include_router(effective_dialog())
    router.include_router(settings_dialog())
    router.include_router(logs_dialog())
    router.include_router(bots_dialog())
    return router
