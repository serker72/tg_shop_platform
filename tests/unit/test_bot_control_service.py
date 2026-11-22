"""Unit-level invariants for BotControlService without Telegram or a database."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.modules.bot_manager.service import TOKEN_RE, BotControlService
from app.shared_kernel.domain.exceptions import DomainError

pytestmark = pytest.mark.req("R-II.8")


class StrictPublisher:
    supports_transactional = False

    def __init__(self) -> None:
        self.publish = AsyncMock(side_effect=RuntimeError("broker unavailable"))


def test_token_format_contract() -> None:
    assert TOKEN_RE.fullmatch("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456")
    assert not TOKEN_RE.fullmatch("123:short")
    assert not TOKEN_RE.fullmatch("token-without-id")


@pytest.mark.asyncio
async def test_publish_failure_is_not_hidden() -> None:
    publisher = StrictPublisher()
    service = BotControlService(AsyncMock(), publisher=publisher)
    bot_id, tenant_id = uuid4(), uuid4()
    cast(Any, service.repo).get = AsyncMock(
        return_value=SimpleNamespace(id=bot_id, tenant_id=tenant_id)
    )
    cast(Any, service)._require = AsyncMock(
        return_value=SimpleNamespace(is_active=True, is_deleted=False)
    )
    with pytest.raises(DomainError, match="Transactional outbox is unavailable") as exc_info:
        await service.start(bot_id, 42)
    assert exc_info.value.code == "transactional_outbox_required"
    publisher.publish.assert_not_awaited()
