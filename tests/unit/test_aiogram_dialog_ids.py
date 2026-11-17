"""Unique aiogram-dialog intent ids (shared_kernel)."""

from __future__ import annotations

import pytest
from aiogram.fsm.state import State, StatesGroup
from aiogram_dialog.api.entities import stack
from structlog.testing import capture_logs

from app.shared_kernel.presentation.aiogram_dialog_ids import (
    MAX_ID,
    SLOTS,
    UniqueIntIdGenerator,
    install_unique_intent_ids,
)

ORIGINAL_MAX = 99 * 100_000_000 + 99_999_999


class _S(StatesGroup):
    main = State()


@pytest.fixture
def restore_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stack, "new_int_id", stack.new_int_id)


def test_many_ids_in_one_second_are_unique() -> None:
    gen = UniqueIntIdGenerator(clock=lambda: 1_700_000_000.0)
    values = [gen() for _ in range(50_000)]
    assert len(set(values)) == len(values)
    assert all(0 <= v <= MAX_ID for v in values)


def test_two_processes_do_not_intersect() -> None:
    now = 1_700_000_000.0
    first = UniqueIntIdGenerator(slot=7, clock=lambda: now)
    second = UniqueIntIdGenerator(slot=8, clock=lambda: now)
    a = {first() for _ in range(10_000)}
    b = {second() for _ in range(10_000)}
    assert not a & b


def test_restart_with_same_slot_does_not_reuse_previous_ids() -> None:
    clock = [1_700_000_000.0]
    old = UniqueIntIdGenerator(slot=3, clock=lambda: clock[0])
    old_ids = {old() for _ in range(100)}
    clock[0] += 101
    new = UniqueIntIdGenerator(slot=3, clock=lambda: clock[0])
    assert not old_ids & {new() for _ in range(100)}


def test_encoded_length_not_longer_than_original() -> None:
    assert len(stack.id_to_str(MAX_ID)) <= len(stack.id_to_str(ORIGINAL_MAX)) == 6
    gen = UniqueIntIdGenerator(slot=SLOTS - 1, clock=lambda: 99_999_999.0)
    assert len(stack.id_to_str(gen())) <= 6


def test_invalid_slot_rejected() -> None:
    with pytest.raises(ValueError):
        UniqueIntIdGenerator(slot=SLOTS)


@pytest.mark.usefixtures("restore_stack")
def test_install_is_idempotent() -> None:
    assert install_unique_intent_ids() is True
    installed = stack.new_int_id
    assert isinstance(installed, UniqueIntIdGenerator)
    assert install_unique_intent_ids() is True
    assert stack.new_int_id is installed


@pytest.mark.usefixtures("restore_stack")
def test_missing_attribute_returns_false_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(stack, "new_int_id")
    with capture_logs() as logs:
        assert install_unique_intent_ids() is False
    assert any(
        e["log_level"] == "warning" and e["event"] == "aiogram_dialog_ids.install_skipped"
        for e in logs
    )


@pytest.mark.usefixtures("restore_stack")
def test_stack_and_push_use_installed_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    gen = UniqueIntIdGenerator(slot=11, clock=lambda: 1_700_000_000.0)

    def tracked() -> int:
        value = gen()
        calls.append(value)
        return value

    monkeypatch.setattr(stack, "new_int_id", tracked)
    st = stack.Stack()
    ctx = st.push(_S.main, None)
    assert [stack.id_to_str(v) for v in calls] == [st.id, ctx.id]
    assert st.id != ctx.id
