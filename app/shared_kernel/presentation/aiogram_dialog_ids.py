"""Collision-free aiogram-dialog stack/intent ids.

aiogram-dialog 2.6 (``aiogram_dialog/api/entities/stack.py``) builds ids as
``int(time()) % 1e8 + randint(0, 99) * 1e8`` -- only 100 distinct ids per second.
Two dialogs opened in one chat within a second may share an id; the child context then
overwrites/removes the parent's one (storage keys are ``bot_id + chat + intent_id``) and
the user gets ``UnknownIntent``.

Both ``Stack._id`` (``default_factory=new_id``) and ``Stack.push`` call ``new_id()``,
which resolves the module global ``new_int_id`` at call time, so replacing
``stack.new_int_id`` covers every place. No other library module imports ``new_int_id``.

Generator: ``id = (c mod PERIOD) * SLOTS + slot`` where

* ``c = max(previous_c + 1, int(time()))`` -- strictly increasing inside the process
  (unique) and anchored to the wall clock, so a restarted process does not reuse ids
  of the previous run (unless it was ahead of the clock by its burst size);
* ``slot`` -- per-process random salt chosen once at install; ids of processes with
  different slots never intersect (``id % SLOTS == slot``).

``PERIOD * SLOTS`` (5e10) < 62**6, so the base62 string stays <= 6 chars like the
original ids (max 9_999_999_999) and callback_data does not grow.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from typing import Final

import structlog

PERIOD: Final = 100_000_000  # same wrap as the library: ~3.17 years of seconds
SLOTS: Final = 500
MAX_ID: Final = PERIOD * SLOTS - 1  # 49_999_999_999 < 62**6 -> <= 6 base62 chars

_log = structlog.get_logger(__name__)


class UniqueIntIdGenerator:
    """Thread/async-safe drop-in replacement for ``stack.new_int_id``."""

    def __init__(self, slot: int | None = None, *, clock: Callable[[], float] = time.time) -> None:
        if slot is None:
            slot = secrets.randbelow(SLOTS)
        if not 0 <= slot < SLOTS:
            raise ValueError(f"slot must be in [0, {SLOTS})")
        self.slot = slot
        self._clock = clock
        self._last = 0
        self._lock = threading.Lock()

    def __call__(self) -> int:
        with self._lock:
            self._last = max(self._last + 1, int(self._clock()))
            counter = self._last
        return (counter % PERIOD) * SLOTS + self.slot


def install_unique_intent_ids() -> bool:
    """Replace aiogram-dialog's id generator (idempotent).

    Returns False (with a warning, no exception) when the expected library attributes
    are missing, e.g. after an aiogram-dialog upgrade.
    """
    try:
        from aiogram_dialog.api.entities import stack
    except ImportError:
        _log.warning("aiogram_dialog_ids.install_skipped", reason="module_not_found")
        return False
    missing = [name for name in ("new_int_id", "new_id", "Stack") if not hasattr(stack, name)]
    if missing:
        _log.warning(
            "aiogram_dialog_ids.install_skipped", reason="attribute_missing", missing=missing
        )
        return False
    current = stack.new_int_id
    if isinstance(current, UniqueIntIdGenerator):
        return True
    generator = UniqueIntIdGenerator()
    stack.new_int_id = generator
    _log.info("aiogram_dialog_ids.installed", slot=generator.slot)
    return True
