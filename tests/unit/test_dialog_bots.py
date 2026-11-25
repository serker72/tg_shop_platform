"""Drive the real shop and bot dialogs through aiogram-dialog test_tools."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from aiogram.fsm.state import State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram_dialog.api.entities import AccessSettings, Data, ShowMode, StartMode
from aiogram_dialog.manager.manager import ManagerImpl
from aiogram_dialog.utils import remove_intent_id

from app.entrypoints.master_bot.texts import Texts
from app.modules.bot_manager.models import Bot
from app.modules.bot_manager.presentation.tg_master.states import BotsSG
from app.modules.bot_manager.service import BotPage
from app.modules.iam.permissions import BOTS_MANAGE, SEED_ROLE_PERMISSIONS, SEED_ROLES
from app.modules.iam.snapshot import PermissionSnapshot
from app.shared_kernel.domain.exceptions import EntityNotFoundError
from tests.unit._master_bot_env import ACTOR, SUPER, VIEWER, Env, Forged, _tenant

pytestmark = pytest.mark.req("R-II.8")


def _service(bots: list[Bot], *, read_only: bool = False) -> AsyncMock:
    service = AsyncMock()

    async def page_for_tenant(actor: int, tenant: UUID, *, page: int) -> BotPage:
        pages = (len(bots) + 7) // 8
        index = max(0, min(page, pages - 1))
        return BotPage(bots[index * 8 : index * 8 + 8], index, pages, read_only)

    async def get_for_tenant(actor: int, tenant: UUID, bot_id: UUID) -> Bot:
        for bot in bots:
            if bot.id == bot_id:
                return bot
        raise EntityNotFoundError("Bot not found", code="bot_not_found")

    service.page_for_tenant.side_effect = page_for_tenant
    service.get_for_tenant.side_effect = get_for_tenant
    service.tenant_is_read_only.return_value = read_only
    return service


@pytest.mark.parametrize(
    "role",
    ["superuser", *(slug for slug, _ in SEED_ROLES), "viewer", "no_permissions"],
)
async def test_main_menu_has_no_bots_button_for_any_role(role: str) -> None:
    tenant = _tenant("My shop", "my-shop")
    if role == "superuser":
        snapshot = SUPER
    elif role == "viewer":
        snapshot = VIEWER
    elif role == "no_permissions":
        snapshot = PermissionSnapshot(user_id=uuid4())
    else:
        snapshot = PermissionSnapshot(
            user_id=uuid4(),
            role_permissions={slug: frozenset({role}) for slug in SEED_ROLE_PERMISSIONS[role]},
            tenants=frozenset({tenant.id}),
        )
    env = Env(snapshot, [tenant])
    await env.start()
    assert env.text() == Texts.main_menu_title()
    buttons = env.buttons()
    assert not any("Боты" in label for label in buttons)
    markup = env.messages.last_message().reply_markup
    for row in markup.inline_keyboard if markup else []:
        for button in row:
            if button.callback_data is not None:
                _, data = remove_intent_id(button.callback_data)
                widget_id, _, _ = data.partition(":")
                assert widget_id != "bots"
    if role == "no_permissions":
        assert buttons == []
    elif role == "viewer":
        assert Texts.btn_logs() in buttons
    else:
        assert Texts.btn_shops() in buttons


async def test_bot_rows_actions_and_instructions() -> None:
    tenant = _tenant("My shop", "my-shop")
    bot = Bot(
        id=uuid4(),
        status="stopped",
        is_active=False,
        name="Sample",
        username="sample_bot",
        telegram_bot_id=123456,
        last_error=None,
    )
    service = _service([bot])
    service.proxy_display.return_value = None
    service.start.side_effect = lambda *a, **kw: setattr(bot, "is_active", True)
    service.stop.side_effect = lambda *a, **kw: setattr(bot, "is_active", False)
    env = Env(SUPER, [tenant], bot_control_service=service)
    await env.start()
    await env.press("Магазины")
    await env.press("My shop (my-shop)")
    await env.press("🤖 Боты")
    assert "⚪ Sample @sample_bot · выкл" in env.buttons()
    await env.forge(bot.id.hex, bot.id.hex, uuid4().hex)
    service.start.assert_not_awaited()
    await env.press("▶️")
    service.start.assert_awaited_once_with(bot.id, ACTOR, tenant_id=tenant.id)
    assert "⚪ Sample @sample_bot · вкл" in env.buttons()
    await env.press("⏹")
    service.stop.assert_awaited_once_with(bot.id, ACTOR, tenant_id=tenant.id)
    await env.press("⚪ Sample @sample_bot · выкл")
    await env.press("🗑 Удалить")
    service.deactivate.assert_not_awaited()
    await env.press("Да, удалить")
    service.deactivate.assert_awaited_once_with(bot.id, ACTOR, tenant_id=tenant.id)
    await env.press("➕ Добавить бота")
    markup = env.messages.last_message().reply_markup
    assert markup is not None
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert any(b.url == "https://t.me/BotFather" for b in buttons)
    assert any(b.copy_text and b.copy_text.text == "/newbot" for b in buttons)
    assert (
        "Скопируйте токен. Нажмите «Ввести токен» и отправьте его на следующем экране."
        in env.text()
    )
    assert "Отправьте токен" not in env.text()
    await env.press("Ввести токен")
    assert "Отправьте токен" in env.text()


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("outcome", ["success", "invalid", "delete_error", "unexpected", "start"])
async def test_token_input_does_not_enter_fsm_or_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, replace: bool, outcome: str
) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import DeleteMessage
    from aiogram.types import Message
    from structlog.testing import capture_logs

    from app.entrypoints.master_bot.middlewares import TEXT_MARKER, ClickLogMiddleware
    from app.shared_kernel.domain.exceptions import DomainError

    secret = ("/" if outcome != "success" else "") + "123456:" + "X" * 35
    if outcome == "start":
        secret = "/start 123456:" + "X" * 35
    activity = SimpleNamespace(log=__import__("unittest.mock", fromlist=["Mock"]).Mock())
    bot = Bot(
        id=uuid4(),
        name="Sample",
        username=None,
        telegram_bot_id=123456,
        status="stopped",
        is_active=False,
        last_error=None,
    )
    tenant = _tenant("My shop", "my-shop")
    service = _service([bot])
    service.proxy_display.return_value = None
    submit = service.replace_token if replace else service.add_bot
    submit.return_value = bot
    if outcome in ("invalid", "start"):
        submit.side_effect = DomainError(secret, code="invalid_token")
    elif outcome == "unexpected":
        submit.side_effect = RuntimeError(secret)
    delete = AsyncMock()
    if outcome == "delete_error":
        delete.side_effect = TelegramBadRequest(
            method=DeleteMessage(chat_id=1, message_id=1), message=secret
        )
    monkeypatch.setattr(Message, "delete", delete)
    env = Env(
        SUPER,
        [tenant],
        bot_control_service=service,
        click_log=ClickLogMiddleware(activity, telegram_bot_id=123),
    )
    await env.start()
    await env.press("Магазины")
    await env.press("My shop (my-shop)")
    await env.press("🤖 Боты")
    if replace:
        await env.press("⚪ Sample · выкл")
        await env.press("🔑 Заменить токен")
    else:
        await env.press("➕ Добавить бота")
        await env.press("Ввести токен")
    with capture_logs() as logs:
        await env.send(secret)
    delete.assert_awaited_once()
    event = activity.log.call_args.args[0]
    if outcome == "start":
        assert event.action_type == "command" and event.button == "/start"
        assert "Telegram отклонил токен" in env.text()
        await env.press("Назад")
        await env.start()
        assert "Магазины" in env.buttons()
    else:
        assert event.action_type == "message" and event.button == TEXT_MARKER
    assert secret not in repr(event)
    if outcome == "delete_error":
        submit.assert_not_awaited()
    else:
        submit.assert_awaited_once()
    storage = env.client.dp.fsm.storage
    assert isinstance(storage, MemoryStorage)
    assert secret not in repr(storage.storage)
    assert secret not in caplog.text + repr(logs)
    assert all(not record.exc_info for record in caplog.records)
    assert all("exc_info" not in entry for entry in logs)
    if outcome == "unexpected":
        assert any(entry["event"] == "bot_token_input_failed" for entry in logs)


async def test_old_card_and_confirmation_cannot_mutate_another_bot() -> None:
    tenant = _tenant("My shop", "my-shop")
    first, second = [
        Bot(
            id=uuid4(),
            name=name,
            username=None,
            telegram_bot_id=i,
            status="stopped",
            is_active=False,
            last_error=None,
        )
        for i, name in enumerate(("First", "Second"), 1)
    ]
    service = _service([first, second])
    service.proxy_display.return_value = None
    env = Env(SUPER, [tenant], bot_control_service=service)
    for label in ("Магазины", "My shop (my-shop)", "🤖 Боты", "⚪ First · выкл"):
        if label == "Магазины":
            await env.start()
        await env.press(label)
    stale = [
        env._data(f"{first.id.hex}:{action}")
        for action in ("start", "stop", "restart", "replace", "delete")
        if action != "stop"
    ]
    await env.press("🗑 Удалить")
    confirm = env._data(f"{first.id.hex}:yes")
    for label in ("Нет", "Назад", "⚪ Second · выкл"):
        await env.press(label)
    for data in stale:
        await env.client.click(env.messages.last_message(), Forged(data))
        assert "Second" in env.text()
    service.start.assert_not_awaited()
    service.restart.assert_not_awaited()
    await env.press("🗑 Удалить")
    await env.client.click(env.messages.last_message(), Forged(confirm))
    service.deactivate.assert_not_awaited()
    await env.press("Да, удалить")
    service.deactivate.assert_awaited_once_with(second.id, ACTOR, tenant_id=tenant.id)


async def test_stale_add_callback_and_input_recheck_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiogram.types import Message

    tenant = _tenant("My shop", "my-shop")
    service = _service([])
    env = Env(SUPER, [tenant], bot_control_service=service)
    await env.start()
    for label in ("Магазины", "My shop (my-shop)", "🤖 Боты", "➕ Добавить бота"):
        await env.press(label)
    service.tenant_is_read_only.return_value = True
    await env.press("Ввести токен")
    assert "Отправьте токен" not in env.text()
    service.tenant_is_read_only.return_value = False
    await env.press("Назад")
    await env.press("➕ Добавить бота")
    await env.press("Ввести токен")
    service.tenant_is_read_only.return_value = True
    delete = AsyncMock()
    monkeypatch.setattr(Message, "delete", delete)
    await env.send("/malformed-secret")
    delete.assert_awaited_once()
    service.add_bot.assert_not_awaited()
    assert "Отправьте токен" not in env.text()


async def test_moderator_scope_rejects_foreign_start_data_and_bot_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own = _tenant("Own shop", "own-shop")
    foreign = _tenant("Foreign shop", "foreign-shop")
    own_bot = Bot(
        id=uuid4(),
        status="stopped",
        is_active=False,
        name="Own bot",
        username="own_bot",
        telegram_bot_id=123456,
        last_error=None,
    )
    foreign_bot_id = uuid4()
    service = _service([own_bot])
    service.proxy_display.return_value = None
    snapshot = PermissionSnapshot(
        user_id=uuid4(),
        role_permissions={BOTS_MANAGE: frozenset({"moderator"})},
        tenants=frozenset({own.id}),
    )
    env = Env(snapshot, [own, foreign], visible=[own], bot_control_service=service)
    await env.start()
    await env.press("Магазины")
    assert "Foreign shop (foreign-shop)" not in env.buttons()
    await env.press("Own shop (own-shop)")
    original_start = ManagerImpl.start

    async def forged_start(
        manager: ManagerImpl,
        state: State,
        data: Data = None,
        mode: StartMode = StartMode.NORMAL,
        show_mode: ShowMode | None = None,
        access_settings: AccessSettings | None = None,
    ) -> None:
        if state == BotsSG.list:
            data = {"tenant_id": str(foreign.id)}
        await original_start(
            manager,
            state,
            data=data,
            mode=mode,
            show_mode=show_mode,
            access_settings=access_settings,
        )

    with monkeypatch.context() as patch:
        patch.setattr(ManagerImpl, "start", forged_start)
        await env.press("🤖 Боты")
    assert "Операция недоступна" in env.text()
    service.page_for_tenant.assert_not_awaited()
    service.tenant_is_read_only.assert_not_awaited()
    await env.press("Назад")
    await env.press("🤖 Боты")
    service.page_for_tenant.assert_awaited_with(ACTOR, own.id, page=0)
    await env.forge("bt", own_bot.id.hex, foreign_bot_id.hex)
    service.start.assert_not_awaited()
    service.stop.assert_not_awaited()
    markup = env.messages.last_message().reply_markup
    assert markup is not None
    own_row = next(row for row in markup.inline_keyboard if row[0].text.startswith("⚪"))
    callback_data = own_row[0].callback_data
    assert callback_data is not None
    await env.client.click(
        env.messages.last_message(),
        Forged(callback_data.replace(own_bot.id.hex, foreign_bot_id.hex)),
    )
    assert "Own bot" in env.buttons()[0] and "Telegram ID" not in env.text()
    own_bot.is_active = True
    await env.forge("bt_start", "bt_start", "bt_stop")
    service.stop.assert_awaited_once_with(own_bot.id, ACTOR, tenant_id=own.id)
    service.stop.reset_mock()
    assert "⏹" in env.buttons()
    await env.forge("bt", own_bot.id.hex, foreign_bot_id.hex)
    service.start.assert_not_awaited()
    service.stop.assert_not_awaited()
    own_bot.is_active = False
    await env.press("⏹")
    service.start.assert_not_awaited()
    service.stop.assert_awaited_once_with(own_bot.id, ACTOR, tenant_id=own.id)
    await env.press("▶️")
    service.start.assert_awaited_once_with(own_bot.id, ACTOR, tenant_id=own.id)


async def test_multi_bot_rows_keep_action_callback_with_same_bot() -> None:
    bots = [
        Bot(
            id=uuid4(),
            status="running" if i % 2 else "stopped",
            is_active=bool(i % 2),
            name=f"Bot {i}",
            username=f"bot{i}",
            telegram_bot_id=i,
        )
        for i in range(10)
    ]
    service = _service(bots)
    env = Env(SUPER, [_tenant("My shop", "my-shop")], bot_control_service=service)
    await env.start()
    await env.press("Магазины")
    await env.press("My shop (my-shop)")
    await env.press("🤖 Боты")
    for page, expected in enumerate((bots[:8], bots[8:])):
        markup = env.messages.last_message().reply_markup
        assert markup is not None
        rows = [row for row in markup.inline_keyboard if row[0].text.startswith(("⚪", "🟢"))]
        assert len(rows) == len(expected)
        for bot, row in zip(expected, rows, strict=True):
            assert len(row) == 2 and bot.name is not None and bot.name in row[0].text
            assert row[1].text == ("⏹" if bot.is_active else "▶️")
            assert all(bot.id.hex in (button.callback_data or "") for button in row)
        selected = 0 if page == 0 else 1
        callback_data = rows[selected][1].callback_data
        assert callback_data is not None
        await env.client.click(env.messages.last_message(), Forged(callback_data))
        action = service.stop if expected[selected].is_active else service.start
        action.assert_awaited_with(expected[selected].id, ACTOR, tenant_id=next(iter(env.store)))
        if page == 0:
            await env.press(">")
    assert ">" not in env.buttons() and "<" in env.buttons()
    del bots[8:]
    # A stale page is clamped even after a callback for the now-hidden Next button.
    await env.forge("prev", "prev", "next")
    assert "Bot 0" in env.buttons()[0]
    assert not ({"<", ">"} & set(env.buttons()))
    service.list_for_tenant.assert_not_awaited()


async def test_bot_list_displays_starting_invalid_token_and_error_icons() -> None:
    statuses = {"starting": "🟡", "invalid_token": "🔑", "error": "🔴"}
    bots = [
        Bot(
            id=uuid4(),
            status=status,
            is_active=status == "starting",
            name=f"Bot {i}",
            username=f"sample{i}_bot",
            telegram_bot_id=i,
        )
        for i, status in enumerate(statuses, start=1)
    ]
    env = Env(SUPER, [_tenant("My shop", "my-shop")], bot_control_service=_service(bots))
    await env.start()
    await env.press("Магазины")
    await env.press("My shop (my-shop)")
    await env.press("🤖 Боты")
    for bot in bots:
        active = "вкл" if bot.is_active else "выкл"
        assert f"{statuses[bot.status]} {bot.name} @{bot.username} · {active}" in env.buttons()


async def test_empty_bot_list_has_no_pager() -> None:
    tenant = _tenant("My shop", "my-shop")
    service = _service([])
    env = Env(SUPER, [tenant], bot_control_service=service)
    await env.start()
    await env.press("Магазины")
    await env.press("My shop (my-shop)")
    await env.press("🤖 Боты")
    service.page_for_tenant.assert_awaited_once_with(ACTOR, tenant.id, page=0)
    assert set(env.buttons()) == {"➕ Добавить бота", "Назад"}


async def test_shop_card_hides_bot_button_without_bots_manage() -> None:
    tenant = _tenant("My shop", "my-shop")
    service = _service([])
    snapshot = PermissionSnapshot(user_id=uuid4(), tenants=frozenset({tenant.id}))
    env = Env(snapshot, [tenant], bot_control_service=service)
    await env.start()
    await env.press("Магазины")
    await env.press("My shop (my-shop)")
    assert "« Назад" in env.buttons()
    assert "🤖 Боты" not in env.buttons()
    service.page_for_tenant.assert_not_awaited()


async def test_read_only_bot_list_and_hidden_shop_entry() -> None:
    tenant = _tenant("My shop", "my-shop")
    service = _service([], read_only=True)
    env = Env(SUPER, [tenant], bot_control_service=service)
    await env.start()
    await env.press("Магазины")
    await env.press("My shop (my-shop)")
    env.snapshot = VIEWER
    await env.forge("t_bots", "t_bots", "t_bots")
    service.page_for_tenant.assert_not_awaited()
    env = Env(SUPER, [tenant], bot_control_service=service)
    await env.start()
    await env.press("Магазины")
    await env.press("My shop (my-shop)")
    await env.press("🤖 Боты")
    assert "только просмотр" in env.text()
    assert "➕ Добавить бота" not in env.buttons()
