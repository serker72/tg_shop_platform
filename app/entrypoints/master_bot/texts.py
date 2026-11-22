"""Texts of the Master Bot UI: msgids (source language ru) resolved through gettext."""

from __future__ import annotations

from app.core.i18n import gettext as _

__all__ = ["Texts"]


class Texts:
    """Lazily translated texts (call at render time, inside the update context)."""

    @staticmethod
    def maintenance() -> str:
        return _("Идут технические работы. Попробуйте позже.")

    @staticmethod
    def main_menu_title() -> str:
        return _("Панель управления платформой")

    @staticmethod
    def no_access() -> str:
        return _("Нет доступа.")

    @staticmethod
    def section_soon() -> str:
        return _("Раздел в разработке")

    @staticmethod
    def btn_shops() -> str:
        return _("Магазины")

    @staticmethod
    def btn_staff() -> str:
        return _("Сотрудники")

    @staticmethod
    def btn_roles() -> str:
        return _("Роли")

    @staticmethod
    def btn_settings() -> str:
        return _("Настройки")

    @staticmethod
    def btn_logs() -> str:
        return _("Логи")
