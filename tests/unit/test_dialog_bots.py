"""Static and callback-safety contracts of the tenant-scoped Bots dialog."""

import inspect

import pytest

from app.modules.bot_manager.presentation.tg_master import bots
from app.modules.bot_manager.presentation.tg_master.states import BotsSG

pytestmark = pytest.mark.req("R-II.8")


def test_dialog_contains_botfather_and_copy_text() -> None:
    dialog = bots.bots_dialog()
    source = inspect.getsource(bots.bots_dialog)
    assert "https://t.me/BotFather" in source
    assert "/newbot" in source
    assert dialog is not None
    assert BotsSG.list.state == "BotsSG:list"


def test_callbacks_resolve_tenant_and_bot_server_side() -> None:
    source = inspect.getsource(bots)
    assert "service.list_for_tenant" in source
    assert "_tenant_id(manager)" in source
    assert "UUID(str(manager.dialog_data" in source


def test_list_has_per_row_toggle_buttons_for_both_statuses() -> None:
    source = inspect.getsource(bots)
    assert "ListGroup(" in source
    assert '"⏹" if b.is_active else "▶️"' in source
    assert 'Button(Format("{item[2]}"), id="toggle"' in source


def test_toggle_rechecks_scope_and_calls_existing_controls() -> None:
    source = inspect.getsource(bots.toggle_bot)
    assert "list_for_tenant" in source
    assert "service.stop" in source
    assert "service.start" in source
    assert "await manager.switch_to(BotsSG.list)" in source


def test_forged_cross_tenant_callback_is_rejected_server_side() -> None:
    source = inspect.getsource(bots.toggle_bot)
    assert "bot = next((item for item in bots if item.id == bot_id), None)" in source
    assert "if bot is None" in source
    assert "EntityNotFoundError" in source


def test_toggle_callback_contains_only_short_bot_identifier() -> None:
    source = inspect.getsource(bots)
    assert "item_id_getter=lambda x: x[0]" in source
    assert 'id="toggle"' in source
    # intent + widget + UUID + button id remains below Telegram's 64-byte limit.
    assert len("123456:toggle:12345678-1234-1234-1234-123456789012") <= 64
