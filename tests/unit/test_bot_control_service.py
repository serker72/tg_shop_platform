"""Unit-level invariants for BotControlService without Telegram or a database."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from aiogram.types import User

from app.modules.bot_manager.models import Bot, Proxy
from app.modules.bot_manager.proxy_service import NoProxyAvailableError
from app.modules.bot_manager.publisher import BotCommandPublisher
from app.modules.bot_manager.service import TOKEN_RE, BotControlService
from app.shared_kernel.domain.exceptions import DomainError

pytestmark = pytest.mark.req("R-II.8")


def test_token_format_contract() -> None:
    assert TOKEN_RE.fullmatch("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456")
    assert not TOKEN_RE.fullmatch("123:short")
    assert not TOKEN_RE.fullmatch("token-without-id")


@pytest.mark.asyncio
async def test_publish_failure_is_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    publisher = AsyncMock(spec=BotCommandPublisher)
    publisher.supports_transactional = False
    publisher.publish.side_effect = RuntimeError("broker unavailable")
    service = BotControlService(AsyncMock(), publisher=publisher)
    bot_id, tenant_id = uuid4(), uuid4()
    monkeypatch.setattr(
        service.repo,
        "get_for_tenant",
        AsyncMock(
            return_value=Bot(id=bot_id, tenant_id=tenant_id, status="stopped", is_active=False)
        ),
    )
    monkeypatch.setattr(
        service,
        "_require",
        AsyncMock(return_value=SimpleNamespace(is_active=True, is_deleted=False)),
    )
    with pytest.raises(DomainError, match="Transactional outbox is unavailable") as exc_info:
        await service.start(bot_id, 42, tenant_id=tenant_id)
    assert exc_info.value.code == "transactional_outbox_required"
    publisher.publish.assert_not_awaited()


@pytest.mark.parametrize("status", ["running", "starting"])
async def test_repeat_start_is_noop_but_restart_publishes(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    service = BotControlService(AsyncMock())
    tenant_id, bot_id = uuid4(), uuid4()
    bot = Bot(id=bot_id, is_active=True, status=status)
    lock, command, audit = AsyncMock(return_value=bot), AsyncMock(), AsyncMock()
    monkeypatch.setattr(service, "_locked_bot", lock)
    monkeypatch.setattr(service, "_command", command)
    monkeypatch.setattr(service, "_audit", audit)
    assert await service.start(bot_id, 42, tenant_id=tenant_id) is bot
    lock.assert_awaited_once_with(bot_id, 42, tenant_id)
    assert bot.status == status
    command.assert_not_awaited()
    audit.assert_not_awaited()
    await service.restart(bot_id, 42, tenant_id=tenant_id)
    command.assert_awaited_once_with("RESTART_BOT", bot, 42)


async def test_permission_denial_precedes_bot_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    service = BotControlService(AsyncMock())
    tenant_id, bot_id = uuid4(), uuid4()
    require = AsyncMock(side_effect=DomainError("denied", code="permission_denied"))
    lock = AsyncMock()
    monkeypatch.setattr(service, "_require", require)
    monkeypatch.setattr(service.repo, "get_for_tenant", lock)
    with pytest.raises(DomainError, match="denied"):
        await service.stop(bot_id, 42, tenant_id=tenant_id)
    require.assert_awaited_once_with(42, tenant_id, write=True)
    lock.assert_not_awaited()


@pytest.mark.parametrize("remaining", [0, 8])
async def test_page_uses_one_snapshot(monkeypatch: pytest.MonkeyPatch, remaining: int) -> None:
    service = BotControlService(AsyncMock())
    tenant_id = uuid4()
    rows = [Bot(id=uuid4()) for _ in range(remaining)]
    fetch = AsyncMock(return_value=(rows, 0, int(bool(remaining))))
    monkeypatch.setattr(
        service,
        "_require",
        AsyncMock(return_value=SimpleNamespace(is_active=True, is_deleted=False)),
    )
    monkeypatch.setattr(service.repo, "snapshot_page_for_tenant", fetch)
    page = await service.page_for_tenant(42, tenant_id, page=1)
    assert (page.items, page.index, page.page_count) == (rows, 0, int(bool(remaining)))
    fetch.assert_awaited_once_with(tenant_id, 8, 1)


async def test_proxy_capacity_race_retries_next_candidate(monkeypatch: pytest.MonkeyPatch) -> None:

    first = Proxy(id=uuid4(), url="socks5://first:1080")
    second = Proxy(id=uuid4(), url="socks5://second:1080")
    pool = AsyncMock()
    selections: list[set[UUID]] = []

    async def select_proxy(
        current_id: UUID | None = None, *, exclude: set[UUID] | None = None
    ) -> Proxy:
        selections.append(set(exclude or ()))
        return first if len(selections) == 1 else second

    pool.get_available_proxy.side_effect = select_proxy
    bot = Bot(id=uuid4())
    pool.assign_proxy.side_effect = [NoProxyAvailableError(), bot]
    service = BotControlService(AsyncMock(), proxy_pool=pool, tenants_use_proxy=True)
    me = User(id=7, is_bot=True, username="bot", first_name="Bot")
    get_me = AsyncMock(return_value=me)
    monkeypatch.setattr(service, "_get_me", get_me)
    monkeypatch.setattr(service.repo, "by_telegram_bot_id", AsyncMock(return_value=None))
    token = "123456:" + "x" * 35
    assert await service._add_with_proxy_retry(token, AsyncMock()) is bot
    assert selections == [set(), {first.id}]
    assert [call.args for call in get_me.await_args_list] == [
        (token, first.url),
        (token, second.url),
    ]
    assignments = [call.args[0] for call in pool.assign_proxy.await_args_list]
    assert [(a.proxy_id, a.url) for a in assignments] == [
        (first.id, first.url),
        (second.id, second.url),
    ]


@pytest.mark.parametrize("unauthorized", [True, False])
async def test_get_me_sanitizes_token_and_closes_session(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, unauthorized: bool
) -> None:
    from aiogram.exceptions import TelegramUnauthorizedError
    from aiogram.methods import GetMe
    from structlog.testing import capture_logs

    from app.modules.bot_manager import service as module

    token = "123456:" + "x" * 35
    proxy = "socks5://user:password@localhost:1080"
    failure = (
        TelegramUnauthorizedError(method=GetMe(), message=token)
        if unauthorized
        else RuntimeError(f"{token} {proxy}")
    )
    http = AsyncMock()
    factory = AsyncMock(side_effect=failure)
    monkeypatch.setattr(module, "TelegramBot", lambda *a, **kw: SimpleNamespace(get_me=factory))
    monkeypatch.setattr(module, "build_bot_session", lambda url: http if url == proxy else None)
    with capture_logs() as logs, pytest.raises(DomainError) as error:
        await BotControlService(AsyncMock())._get_me(token, proxy)
    assert error.value.code == ("invalid_token" if unauthorized else "telegram_unavailable")
    assert error.value.__suppress_context__ and error.value.__cause__ is None
    assert token not in str(error.value) + caplog.text + str(logs)
    assert proxy not in str(error.value) + caplog.text + str(logs)
    factory.assert_awaited_once_with(request_timeout=10)
    http.__aexit__.assert_awaited_once()
