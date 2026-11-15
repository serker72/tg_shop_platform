"""FSM states of the shops and staff dialogs."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup

__all__ = ["StaffSG", "TenantsSG"]


class TenantsSG(StatesGroup):
    list = State()
    deleted = State()
    card = State()
    create_name = State()
    create_slug = State()
    rename = State()
    settings = State()
    language = State()
    chat_input = State()
    confirm = State()


class StaffSG(StatesGroup):
    list = State()
    add = State()
    card = State()
    tenants = State()
    confirm = State()
