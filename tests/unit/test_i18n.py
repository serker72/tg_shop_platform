"""gettext skeleton: compiled ``ru`` catalog, coverage of the code strings (task 04)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from app.core.i18n import LOCALES_DIR, build_i18n, gettext

pytestmark = pytest.mark.req("R-II.3")

APP_DIR = Path(__file__).resolve().parents[2] / "app"
RU_PO = LOCALES_DIR / "ru" / "LC_MESSAGES" / "messages.po"


def test_missing_locales_dir_gives_none(tmp_path: Path) -> None:
    assert build_i18n(tmp_path / "absent") is None


def test_ru_catalog_compiled_and_loaded() -> None:
    i18n = build_i18n()
    assert i18n is not None and "ru" in i18n.available_locales


def test_gettext_outside_update_returns_msgid() -> None:
    assert gettext("Логи") == "Логи"


def test_ru_catalog_contains_all_code_strings() -> None:
    extract: Any = importlib.import_module("babel.messages.extract")
    pofile: Any = importlib.import_module("babel.messages.pofile")
    found = {
        item[2] for item in extract.extract_from_dir(str(APP_DIR), method_map=[("**.py", "python")])
    }
    with RU_PO.open("rb") as handle:
        catalog = pofile.read_po(handle)
    known = {message.id for message in catalog if message.id}
    assert found, "no strings extracted"
    assert found <= known, f"run make i18n-extract i18n-update i18n-compile: {found - known}"
