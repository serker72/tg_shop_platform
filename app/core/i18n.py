"""gettext skeleton of the Telegram UI (decision Q1/Q16: ``aiogram.utils.i18n``).

The source language of the msgids is Russian (the only UI language of milestone II), so a
missing catalog returns the msgid itself. Catalogs of other languages are added to
``app/locales/<lang>/LC_MESSAGES/messages.{po,mo}``; handlers never hardcode texts and call
:func:`gettext` (``_``) through ``app/entrypoints/*/texts.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from aiogram.utils.i18n import I18n

__all__ = ["DEFAULT_LOCALE", "I18N_DOMAIN", "LOCALES_DIR", "build_i18n", "gettext"]

LOCALES_DIR: Final = Path(__file__).resolve().parent.parent / "locales"
DEFAULT_LOCALE: Final = "ru"
I18N_DOMAIN: Final = "messages"


def build_i18n(path: Path = LOCALES_DIR) -> I18n:
    """Process-wide translator over ``app/locales`` (default locale ``ru``)."""
    return I18n(path=path, default_locale=DEFAULT_LOCALE, domain=I18N_DOMAIN)


def gettext(msgid: str) -> str:
    """Translate ``msgid`` with the translator of the current update (msgid outside one)."""
    i18n = I18n.get_current(no_error=True)
    if i18n is None:
        return msgid
    return i18n.gettext(msgid)
