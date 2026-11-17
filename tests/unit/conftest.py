"""Unit-test fixtures shared by the Master Bot dialog tests."""

from __future__ import annotations

import itertools

import pytest
from aiogram_dialog.api.entities import stack


@pytest.fixture(autouse=True)
def unique_dialog_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collision-free aiogram-dialog stack/intent ids.

    ``stack.new_int_id`` is ``time() % 1e8 + randint(0, 99) * 1e8``: two dialogs started
    within one second share an id with probability 1/100 (and pytest-randomly reseeds
    ``random`` per test), so a child dialog's ``done`` removed the parent's context ->
    ``UnknownIntent`` in forged-callback tests. A counter keeps ids unique.
    """
    counter = itertools.count(1)
    monkeypatch.setattr(stack, "new_int_id", lambda: next(counter))
