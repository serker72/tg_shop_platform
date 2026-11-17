"""Unit-test fixtures shared by the Master Bot dialog tests."""

from __future__ import annotations

import pytest
from aiogram_dialog.api.entities import stack

from app.shared_kernel.presentation.aiogram_dialog_ids import install_unique_intent_ids


@pytest.fixture(autouse=True)
def unique_dialog_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collision-free aiogram-dialog stack/intent ids.

    ``stack.new_int_id`` is ``time() % 1e8 + randint(0, 99) * 1e8``: two dialogs started
    within one second share an id with probability 1/100 (and pytest-randomly reseeds
    ``random`` per test), so a child dialog's ``done`` removed the parent's context ->
    ``UnknownIntent`` in forged-callback tests. The production generator from
    shared_kernel keeps ids unique: it is installed before every test and the previous
    ``new_int_id`` is restored afterwards. Tests of the installer itself
    (``test_aiogram_dialog_ids.py::restore_stack``) put the library original back
    explicitly, since inside a test this fixture has already replaced it.
    """
    monkeypatch.setattr(stack, "new_int_id", stack.new_int_id)
    assert install_unique_intent_ids()
