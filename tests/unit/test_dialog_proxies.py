"""Acceptance checks for the proxy section via aiogram-dialog test_tools."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import UUID, uuid4

import pytest
from aiogram.exceptions import ClientDecodeError, TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import DeleteMessage
from aiogram.types import Message
from aiogram_dialog import setup_dialogs
from aiogram_dialog.context.storage import StorageProxy
from aiogram_dialog.test_tools import BotClient
from structlog.testing import capture_logs

from app.entrypoints.master_bot import app as master_app
from app.entrypoints.master_bot.middlewares import AuthMiddleware, MaintenanceMiddleware
from app.entrypoints.master_bot.texts import Texts
from app.modules.bot_manager.models import PROXY_STATUS_ACTIVE, PROXY_STATUS_BANNED
from app.modules.bot_manager.presentation.tg_master import proxies
from app.modules.bot_manager.presentation.tg_master.states import ProxiesSG
from app.modules.bot_manager.proxy_service import (
    INVALID_PROXY_URL,
    PROXY_EXISTS,
    PROXY_STATUS_CHANGED,
    ProxyPoolService,
    ProxySummary,
    ProxyVersion,
)
from app.modules.iam.permissions import PROXIES_MANAGE, SETTINGS_MANAGE
from app.modules.iam.snapshot import PermissionSnapshot
from app.shared_kernel.domain.exceptions import DomainError, PermissionDeniedError
from tests.unit._master_bot_env import ACTOR, VIEWER, Env, Forged

pytestmark = pytest.mark.req("R-II.14")

_SECRET = "socks5://sample-login:sample-password@proxy.invalid:1080"
_ADDRESS = "socks5://proxy.invalid:1080"
_MOMENT = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
_STALE = "Карточка прокси устарела. Повторите действие."


@pytest.fixture
def proxy() -> ProxySummary:
    return ProxySummary(
        id=uuid4(),
        label="Тестовый прокси",
        masked_url=_ADDRESS,
        version=ProxyVersion(PROXY_STATUS_ACTIVE, _MOMENT),
        strike_count=2,
        bot_count=3,
        bot_limit=20,
        last_ban_at=_MOMENT,
    )


async def _open(*proxies: ProxySummary) -> tuple[Env, AsyncMock]:
    service = AsyncMock(spec=ProxyPoolService)
    service.list_proxies.return_value = list(proxies)
    index = {proxy.id: proxy for proxy in proxies}

    async def get(proxy_id: UUID, actor: int) -> ProxySummary:
        assert actor == ACTOR
        return index[proxy_id]

    service.get_proxy.side_effect = get
    env = Env(
        PermissionSnapshot(user_id=uuid4(), global_allow=frozenset({PROXIES_MANAGE})),
        proxy_pool_service=service,
    )
    await env.start()
    await env.press("Прокси")
    service.list_proxies.assert_awaited_once_with(ACTOR)
    return env, service


def _assert_no_secret(*surfaces: str) -> None:
    for surface in surfaces:
        assert _SECRET not in surface
        assert "sample-login" not in surface
        assert "sample-password" not in surface


def _assert_no_fsm_secret(env: Env) -> None:
    storage = env.client.dp.fsm.storage
    assert isinstance(storage, MemoryStorage)
    _assert_no_secret(repr(storage.storage))


def _assert_secret_is_hidden(env: Env) -> None:
    _assert_no_secret(env.text(), "\n".join(env.buttons()))
    _assert_no_fsm_secret(env)


async def _open_card(env: Env, proxy: ProxySummary) -> None:
    await env.client.click(env.messages.last_message(), Forged(env._data(f"px:{proxy.id.hex}")))


async def test_proxy_menu_requires_global_permission() -> None:
    service = AsyncMock(spec=ProxyPoolService)
    env = Env(VIEWER, proxy_pool_service=service)
    await env.start()
    assert "Прокси" not in env.buttons()
    await env.forge("logs", "logs", "proxies")
    service.list_proxies.assert_not_awaited()


async def test_proxy_list_and_card_render_masked_details(proxy: ProxySummary) -> None:
    legacy = replace(proxy, id=uuid4(), label=_SECRET)
    env, service = await _open(proxy, legacy)
    details = f"{_ADDRESS} · Активен · штрафы: 2 · боты: 3/20 · бан: 02.01.2026 03:04 UTC"
    assert f"{proxy.label} · {details}" in env.buttons()
    assert f"— · {details}" in env.buttons()
    _assert_secret_is_hidden(env)

    await env.press(f"— · {details}")
    service.get_proxy.assert_awaited_with(legacy.id, ACTOR)
    assert f"— · {details}" in env.text()
    _assert_secret_is_hidden(env)


@pytest.mark.parametrize("window", ["list", "card"])
async def test_open_proxy_dialog_hides_data_when_read_is_denied(
    proxy: ProxySummary, window: str
) -> None:
    env, service = await _open(proxy)
    if window == "list":
        await _open_card(env, proxy)
        service.list_proxies.side_effect = PermissionDeniedError("internal detail")
        await env.press("« Назад")
        service.list_proxies.assert_awaited_with(ACTOR)
        assert "➕ Добавить прокси" not in env.buttons()
    else:
        await _open_card(env, proxy)
        service.get_proxy.side_effect = PermissionDeniedError("internal detail")
        await env.press("Заблокировать")
        service.get_proxy.assert_awaited_with(proxy.id, ACTOR)
        assert "Да" not in env.buttons()
        service.ban.assert_not_awaited()
        # A denied callback also refreshes the window through its getter.
        assert "Заблокировать" not in env.buttons()
    assert "Нет доступа." in env.text()
    assert _ADDRESS not in env.text() + repr(env.buttons())
    assert "internal detail" not in env.text()


async def test_proxy_input_deletes_before_add_and_renders_masked(
    monkeypatch: pytest.MonkeyPatch, proxy: ProxySummary
) -> None:
    env, service = await _open(proxy)
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(Message, "delete", delete)

    async def add(url: str, label: str, actor: int) -> UUID:
        delete.assert_awaited_once()
        assert (url, label, actor) == (_SECRET, proxy.label, ACTOR)
        _assert_no_fsm_secret(env)
        return proxy.id

    service.add.side_effect = add
    await env.press("➕ Добавить прокси")
    await env.send(_SECRET)  # Wrong step: never a label.
    assert "Некорректное название" in env.text()
    _assert_secret_is_hidden(env)
    delete.assert_awaited_once()
    delete.reset_mock()
    service.add.assert_not_awaited()
    await env.send(proxy.label or "")
    await env.send(_SECRET)
    service.add.assert_awaited_once()
    assert _ADDRESS in env.text()
    _assert_secret_is_hidden(env)


@pytest.mark.parametrize("api_error", [False, True])
async def test_proxy_input_does_not_write_when_delete_fails(
    monkeypatch: pytest.MonkeyPatch, api_error: bool
) -> None:
    env, service = await _open()
    delete = AsyncMock(return_value=False)
    if api_error:
        delete.side_effect = TelegramBadRequest(DeleteMessage(chat_id=ACTOR, message_id=1), _SECRET)
    monkeypatch.setattr(Message, "delete", delete)
    await env.press("➕ Добавить прокси")
    await env.send("Тестовый прокси")
    await env.send(f"/start {_SECRET}")
    delete.assert_awaited_once()
    service.add.assert_not_awaited()
    assert "Не удалось удалить сообщение" in env.text()
    assert "Отправьте URL прокси" in env.text()
    _assert_secret_is_hidden(env)


@pytest.mark.parametrize("case", ["deleted", "telegram_refused", "ordinary_window"])
async def test_maintenance_deletes_only_pending_proxy_url(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    env, service = await _open()
    assert env.snapshot.decide(PROXIES_MANAGE).allowed
    assert not env.snapshot.decide(SETTINGS_MANAGE).allowed
    loader = AsyncMock(return_value=env.snapshot)
    gate = AsyncMock()
    gate.is_on.return_value = False
    env.client.dp.update.outer_middleware(AuthMiddleware(loader))
    env.client.dp.update.outer_middleware(MaintenanceMiddleware(gate))
    if case != "ordinary_window":
        await env.press("➕ Добавить прокси")
        await env.send("Тестовый прокси")
        assert "Отправьте URL прокси" in env.text()
    else:
        assert "Пул прокси пуст." in env.text()

    gate.is_on.return_value = True
    delete, send_stub = AsyncMock(return_value=True), AsyncMock()
    message_id = env.client.last_message_id + 1
    if case == "telegram_refused":
        delete.side_effect = TelegramBadRequest(
            DeleteMessage(chat_id=ACTOR, message_id=message_id), _SECRET
        )
    monkeypatch.setattr(env.client.bot, "delete_message", delete)
    monkeypatch.setattr(env.client.bot, "send_message", send_stub)
    service.reset_mock()
    await env.client.send(_SECRET)

    loader.assert_awaited_with(ACTOR)
    assert service.mock_calls == []
    expected = Texts.maintenance()
    if case == "ordinary_window":
        delete.assert_not_awaited()
    else:
        delete.assert_awaited_once_with(ACTOR, message_id)
        if case == "telegram_refused":
            expected += "\n\nНе удалось удалить сообщение. Удалите его вручную."
    send_stub.assert_awaited_once_with(ACTOR, expected)
    _assert_no_secret(send_stub.call_args.args[1])
    _assert_no_fsm_secret(env)


@pytest.mark.parametrize(
    ("text", "should_delete", "force_parse_error"),
    [
        (_SECRET.replace("@", "@["), True, False),
        ("so\tck\rs\n5://proxy.invalid:1080", True, True),
        ("Тестовый прокси @ склад", False, False),
        ("Тестовый прокси @ склад", False, True),
    ],
    ids=["unparseable_url", "normalized_url", "invalid_label", "invalid_label_fallback"],
)
async def test_proxy_label_step_deletes_malformed_url_but_not_ordinary_label(
    monkeypatch: pytest.MonkeyPatch, text: str, should_delete: bool, force_parse_error: bool
) -> None:
    if force_parse_error:
        parse = Mock(side_effect=ValueError("synthetic parser error"))
        monkeypatch.setattr(proxies, "urlsplit", parse)
    env, service = await _open()
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(Message, "delete", delete)
    await env.press("➕ Добавить прокси")
    await env.send(text)

    if force_parse_error:
        parse.assert_called_once_with(text)
    if should_delete:
        delete.assert_awaited_once()
    else:
        delete.assert_not_awaited()
    service.add.assert_not_awaited()
    assert "Некорректное название" in env.text()
    assert "Введите название прокси" in env.text()
    _assert_secret_is_hidden(env)


async def test_proxy_delete_decode_error_does_not_write_or_log_response(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    env, service = await _open()
    detail = "synthetic decode detail"
    delete = AsyncMock(side_effect=ClientDecodeError(detail, ValueError(detail), {"url": _SECRET}))
    monkeypatch.setattr(Message, "delete", delete)
    await env.press("➕ Добавить прокси")
    await env.send("Тестовый прокси")
    with capture_logs() as logs:
        await env.send(_SECRET)

    delete.assert_awaited_once()
    service.add.assert_not_awaited()
    assert "Не удалось удалить сообщение. Удалите его вручную и повторите ввод." in env.text()
    assert "Отправьте URL прокси" in env.text()
    diagnostic = repr(logs) + caplog.text
    _assert_no_secret(diagnostic)
    assert detail not in diagnostic + env.text()
    assert all("exc_info" not in entry and "exception" not in entry for entry in logs)
    assert all(record.exc_info is None for record in caplog.records)
    _assert_secret_is_hidden(env)


@pytest.mark.parametrize("case", ["auth_denied", "state_changes_under_lock", "deletion_holds_lock"])
async def test_dispatcher_maintenance_respects_auth_and_dialog_lock(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    # Keep production router, middleware order and dialog setup; only replace I/O.
    setup = Mock(wraps=setup_dialogs)
    monkeypatch.setattr(master_app, "setup_dialogs", setup)
    storage = MemoryStorage()
    monkeypatch.setattr(master_app, "RedisStorage", Mock(return_value=storage))
    admitted = PermissionSnapshot(user_id=uuid4(), global_allow=frozenset({PROXIES_MANAGE}))
    loader = AsyncMock(return_value=admitted)
    at_gate = asyncio.Event()

    async def maintenance_on() -> bool:
        at_gate.set()
        return True

    gate = Mock(is_on=AsyncMock(side_effect=maintenance_on))
    service = AsyncMock(spec=ProxyPoolService)
    container = MagicMock()
    container.return_value.__aenter__.return_value.get = AsyncMock(return_value=service)
    activity = Mock()
    dispatcher = master_app.build_dispatcher(
        redis=Mock(),
        container=container,
        loader=loader,
        gate=gate,
        activity=activity,
        telegram_bot_id=1,
    )
    client = BotClient(dispatcher, user_id=ACTOR, chat_id=ACTOR)
    delete, send = AsyncMock(return_value=True), AsyncMock()
    monkeypatch.setattr(client.bot, "delete_message", delete)
    monkeypatch.setattr(client.bot, "send_message", send)

    # Seed the open input step and acquire the lock using the isolation supplied to dialogs.
    dialog_storage = StorageProxy(
        storage=storage,
        events_isolation=setup.call_args.kwargs["events_isolation"],
        bot=client.bot,
        user_id=ACTOR,
        chat_id=ACTOR,
        thread_id=None,
        business_connection_id=None,
        state_groups={ProxiesSG.__name__: ProxiesSG},
    )
    stack = await dialog_storage.load_stack()
    try:
        context = stack.push(ProxiesSG.add_url, None)
        context.dialog_data["label"] = "Тестовый прокси"
        await dialog_storage.save_context(context)
        await dialog_storage.save_stack(stack)
    finally:
        await dialog_storage.unlock()  # type: ignore[no-untyped-call]

    if case == "auth_denied":
        loader.return_value = PermissionSnapshot(user_id=None)
        await client.send(_SECRET)
        loader.assert_awaited_once_with(ACTOR)
        gate.is_on.assert_not_awaited()
        send.assert_not_awaited()
        delete.assert_not_awaited()
        assert service.mock_calls == []
        activity.log.assert_not_called()
        # Positive control: the same open step deletes when Auth admits the operator.
        loader.return_value = admitted
        await client.send(_SECRET)
        delete.assert_awaited_once_with(ACTOR, client.last_message_id)
        activity.log.assert_called_once()
    elif case == "deletion_holds_lock":
        deleting, finish_delete, deleted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        acquiring, acquired = asyncio.Event(), asyncio.Event()

        async def pause_delete(chat_id: int, message_id: int) -> bool:
            deleting.set()
            await finish_delete.wait()
            deleted.set()
            return True

        async def compete_for_lock() -> None:
            acquiring.set()
            try:
                await dialog_storage.load_stack()
                assert deleted.is_set(), "dialog acquired its FSM lock before deletion finished"
                acquired.set()
            finally:
                await dialog_storage.unlock()  # type: ignore[no-untyped-call]

        delete.side_effect = pause_delete
        async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
            tasks.create_task(client.send(_SECRET))
            try:
                await deleting.wait()
                tasks.create_task(compete_for_lock())
                await acquiring.wait()
                assert not deleted.is_set()
                assert not acquired.is_set()
            finally:
                finish_delete.set()
            await acquired.wait()
        delete.assert_awaited_once_with(ACTOR, client.last_message_id)
    else:
        async with asyncio.TaskGroup() as tasks:
            await dialog_storage.load_stack()
            try:
                update = tasks.create_task(client.send(_SECRET))
                await asyncio.wait_for(at_gate.wait(), timeout=2)
                assert not update.done()
                send.assert_not_awaited()
                delete.assert_not_awaited()
                context.state = ProxiesSG.add_label
                await dialog_storage.save_context(context)
            finally:
                await dialog_storage.unlock()  # type: ignore[no-untyped-call]
            await asyncio.wait_for(update, timeout=2)
        delete.assert_not_awaited()

    send.assert_awaited_once_with(ACTOR, Texts.maintenance())
    assert service.mock_calls == []
    _assert_no_secret(repr(storage.storage))


async def test_proxy_url_on_label_step_stays_unsaved_when_telegram_refuses_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, service = await _open()
    delete = AsyncMock(
        side_effect=TelegramBadRequest(DeleteMessage(chat_id=ACTOR, message_id=1), _SECRET)
    )
    monkeypatch.setattr(Message, "delete", delete)
    await env.press("➕ Добавить прокси")
    await env.send(_SECRET.replace("socks5://", "ftp://"))

    delete.assert_awaited_once()
    service.add.assert_not_awaited()
    assert "Некорректное название прокси. Не вводите здесь URL." in env.text()
    assert "Не удалось удалить сообщение. Удалите его вручную." in env.text()
    assert "Введите название прокси" in env.text()
    _assert_secret_is_hidden(env)


async def test_unexpected_proxy_add_error_is_logged_without_secret_or_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    env, service = await _open()
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(Message, "delete", delete)

    async def fail(url: str, label: str, actor: int) -> UUID:
        delete.assert_awaited_once()
        raise RuntimeError(f"synthetic add failure: {url}")

    service.add.side_effect = fail
    await env.press("➕ Добавить прокси")
    await env.send("Тестовый прокси")
    with capture_logs() as logs:
        await env.send(_SECRET)

    delete.assert_awaited_once()
    service.add.assert_awaited_once_with(_SECRET, "Тестовый прокси", ACTOR)
    assert [log for log in logs if log["event"] == "proxy_url_input_failed"] == [
        {"event": "proxy_url_input_failed", "log_level": "warning"}
    ]
    _assert_no_secret(repr(logs), caplog.text)
    assert all("exc_info" not in log and "exception" not in log for log in logs)
    assert all(record.exc_info is None for record in caplog.records)
    assert "Traceback" not in repr(logs) + caplog.text
    assert "Не удалось выполнить действие." in env.text()
    assert "Отправьте URL прокси" in env.text()
    assert "synthetic add failure" not in env.text()
    _assert_secret_is_hidden(env)


@pytest.mark.parametrize(
    ("code", "notice"),
    [
        (INVALID_PROXY_URL, "Неверный адрес прокси. Допустимые схемы: socks5, socks4 и http."),
        (PROXY_EXISTS, "Такой прокси уже добавлен."),
    ],
)
async def test_proxy_add_errors_are_localized_without_echoing_input(
    monkeypatch: pytest.MonkeyPatch, code: str, notice: str
) -> None:
    env, service = await _open()
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(Message, "delete", delete)
    service.add.side_effect = DomainError(_SECRET, code=code)
    label = "Тестовый прокси"
    await env.press("➕ Добавить прокси")
    await env.send(label)
    url = _SECRET.replace("socks5://", "ftp://") if code == INVALID_PROXY_URL else _SECRET
    await env.send(url)
    delete.assert_awaited_once()
    service.add.assert_awaited_once_with(url, label, ACTOR)
    assert notice in env.text()
    assert "Отправьте URL прокси" in env.text()
    _assert_secret_is_hidden(env)


@pytest.mark.parametrize(
    ("status", "button", "method", "question"),
    [
        (PROXY_STATUS_ACTIVE, "Заблокировать", "ban", "Назначенные боты будут отвязаны."),
        (PROXY_STATUS_BANNED, "Активировать", "activate", "Вернуть прокси в активный пул?"),
    ],
)
async def test_proxy_confirmation_cancel_and_old_nonce_cannot_apply(
    proxy: ProxySummary, status: str, button: str, method: str, question: str
) -> None:
    proxy = replace(proxy, version=ProxyVersion(status, _MOMENT))
    env, service = await _open(proxy)
    await _open_card(env, proxy)
    await env.press(button)
    assert question in env.text()
    if method == "ban":
        assert env.text().splitlines() == [
            f"Заблокировать прокси? {question}",
            f"{proxy.label} · {_ADDRESS}",
        ]
    _assert_secret_is_hidden(env)
    old = env._data("pc:")
    await env.press("Нет")
    assert button in env.buttons()
    service.ban.assert_not_awaited()
    service.activate.assert_not_awaited()

    await env.press(button)
    assert env._data("pc:") != old
    await env.client.click(env.messages.last_message(), Forged(old))
    service.ban.assert_not_awaited()
    service.activate.assert_not_awaited()
    await env.press("Да")
    getattr(service, method).assert_awaited_once_with(proxy.id, ACTOR, expected=proxy.version)
    assert "Статус прокси обновлён." in env.text()


async def test_proxy_confirmation_sends_original_version_after_concurrent_change(
    proxy: ProxySummary,
) -> None:
    env, service = await _open(proxy)
    await _open_card(env, proxy)
    await env.press("Заблокировать")
    assert "Да" in env.buttons()
    newer = replace(proxy.version, updated_at=_MOMENT + timedelta(seconds=1))
    changed = replace(proxy, version=newer)
    service.get_proxy.side_effect = None
    service.get_proxy.return_value = changed
    service.ban.side_effect = DomainError("internal stale version", code=PROXY_STATUS_CHANGED)

    await env.press("Да")
    service.ban.assert_awaited_once_with(proxy.id, ACTOR, expected=proxy.version)
    service.activate.assert_not_awaited()
    assert _STALE in env.text()
    assert "Статус прокси обновлён." not in env.text()
    assert "internal stale version" not in env.text()
