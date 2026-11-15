"""gettext skeleton of the Telegram UI (decision Q1/Q16: ``aiogram.utils.i18n`` + Babel).

The source language of the msgids is Russian (the only UI language of milestone II): an
empty ``msgstr`` or a missing catalog returns the msgid itself. Catalogs live in
``app/locales/<lang>/LC_MESSAGES/messages.{po,mo}`` (``make i18n-extract`` ->
``i18n-update`` -> ``i18n-compile``; the compiled ``.mo`` is committed, so the image needs
no Babel). Handlers never hardcode texts and call :func:`gettext` (``_``) through
``app/entrypoints/*/texts.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import structlog
from aiogram.utils.i18n import I18n

__all__ = ["DEFAULT_LOCALE", "I18N_DOMAIN", "LOCALES_DIR", "build_i18n", "gettext"]

LOCALES_DIR: Final = Path(__file__).resolve().parent.parent / "locales"
DEFAULT_LOCALE: Final = "ru"
I18N_DOMAIN: Final = "messages"

_log = structlog.get_logger(__name__)


def build_i18n(path: Path = LOCALES_DIR) -> I18n | None:
    """Translator over ``path`` (default locale ``ru``); ``None`` if the directory is absent."""
    if not path.is_dir():
        _log.warning("i18n_locales_dir_missing", path=str(path))
        return None
    return I18n(path=path, default_locale=DEFAULT_LOCALE, domain=I18N_DOMAIN)


def gettext(msgid: str) -> str:
    """Translate ``msgid`` with the translator of the current update (msgid outside one)."""
    i18n = I18n.get_current(no_error=True)
    if i18n is None:
        return msgid
    return i18n.gettext(msgid)
