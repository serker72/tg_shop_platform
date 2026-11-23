"""FSM states of the shops, staff and roles dialogs."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup

__all__ = ["EffectiveSG", "OverridesSG", "RolesSG", "StaffSG", "TenantsSG"]


class TenantsSG(StatesGroup):
    list = State()
    timezone_presets = State()
    timezone_search = State()
    timezone_results = State()
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
    timezone_presets = State()
    onboarding = State()
    onboarding_results = State()
    add = State()
    card = State()
    tenants = State()
    roles = State()
    confirm = State()


class RolesSG(StatesGroup):
    list = State()
    card = State()
    create_name = State()
    create_slug = State()
    rename = State()
    permissions = State()
    confirm = State()


class OverridesSG(StatesGroup):
    list = State()
    item = State()
    module = State()
    permission = State()
    effect = State()
    scope = State()
    expires = State()
    reason = State()
    confirm = State()
    delete = State()


class EffectiveSG(StatesGroup):
    list = State()
    tenant = State()
    shop = State()
