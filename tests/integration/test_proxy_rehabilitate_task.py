"""``proxy.rehabilitate`` handler against the test PostgreSQL: quarantine -> active, idempotent."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.config import ProxySettings
from app.modules.bot_manager.models import Proxy
from app.modules.bot_manager.proxy_service import proxy_url_hash
from app.modules.bot_manager.tasks import rehabilitate_proxies

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.15")]


def _status(name: str) -> Any:
    column_type = Proxy.__table__.c.status.type
    enum_class = getattr(column_type, "enum_class", None)
    if enum_class is not None:
        return next(m for m in enum_class if name in (str(m.value).lower(), m.name.lower()))
    return next(e for e in (getattr(column_type, "enums", None) or [name]) if e.lower() == name)


def _text(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


async def test_rehabilitate_task_is_idempotent(clean_db: AsyncEngine, redis_client: Any) -> None:
    factory = async_sessionmaker(clean_db, expire_on_commit=False)
    settings = ProxySettings.model_validate({"proxy_quarantine_days": 7})
    old, fresh = "socks5://10.9.8.7:1080", "socks5://10.9.8.8:1080"
    async with factory() as session:
        session.add_all([Proxy(url=url, url_hash=proxy_url_hash(url)) for url in (old, fresh)])
        await session.commit()
    async with factory() as session:
        for url, days in ((old, 8), (fresh, 1)):
            await session.execute(
                update(Proxy)
                .where(Proxy.url_hash == proxy_url_hash(url))
                .values(
                    status=_status("quarantine"),
                    strike_count=2,
                    last_ban_at=func.now() - timedelta(days=days),
                )
            )
        await session.commit()

    assert await rehabilitate_proxies(factory, settings, redis_client) == 1
    assert await rehabilitate_proxies(factory, settings, redis_client) == 0

    async with factory() as session:
        result = await session.execute(select(Proxy.url_hash, Proxy.status))
        rows: dict[Any, str] = {row[0]: _text(row[1]) for row in result.all()}
    assert rows[proxy_url_hash(old)] == "active"
    assert rows[proxy_url_hash(fresh)] == "quarantine"
