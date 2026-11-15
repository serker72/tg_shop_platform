"""Integration: Master Bot permission loader / username sync on PostgreSQL + Redis DB 15 and
click-log through ``LogBuffer`` into ``user_activity`` and files (task 04)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from aiogram.types import Chat, Message, Update, User
from dishka import AsyncContainer, make_async_container
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.entrypoints.master_bot.app import permission_loader, username_sync
from app.entrypoints.master_bot.middlewares import TEXT_MARKER, ClickLogMiddleware
from app.modules.iam.ioc import IamProvider
from app.modules.iam.models import User as IamUser
from app.modules.iam.permissions import USERS_VIEW
from app.modules.logging.buffer import LogBuffer
from app.modules.logging.files import DailyFileWriter
from app.modules.logging.metrics import LogsDroppedCounter
from app.modules.logging.models import UserActivity
from app.shared_kernel.infrastructure.database import build_session_factory
from app.shared_kernel.infrastructure.providers import DatabaseProvider, RedisProvider
from tests.integration.test_permission_service import grant_role

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.3", "S-II.2")]

BOT_TOKEN = "123456789:AAH-secretTokenValue_abcdefghijklmnop"


def _tg() -> int:
    return uuid4().int >> 80


@pytest.fixture
async def container(settings: Settings, clean_db: AsyncEngine) -> AsyncIterator[AsyncContainer]:
    built = make_async_container(
        DatabaseProvider(), RedisProvider(), IamProvider(), context={Settings: settings}
    )
    try:
        yield built
    finally:
        await built.close()


async def _create_users(engine: AsyncEngine) -> dict[str, int]:
    ids = {name: _tg() for name in ("superuser", "admin", "inactive", "plain")}
    factory = build_session_factory(engine)
    async with factory() as session:
        session.add(IamUser(telegram_user_id=ids["superuser"], is_superuser=True))
        session.add(IamUser(telegram_user_id=ids["inactive"], is_active=False, is_superuser=True))
        session.add(IamUser(telegram_user_id=ids["plain"]))
        admin = IamUser(telegram_user_id=ids["admin"], telegram_user_name="old")
        session.add(admin)
        await session.flush()
        await grant_role(session, admin, "admin")
        await session.commit()
    return ids


async def test_permission_loader_real_db(
    container: AsyncContainer, clean_db: AsyncEngine, redis_client: Any
) -> None:
    ids = await _create_users(clean_db)
    load = permission_loader(container)
    assert not (await load(_tg())).exists  # unknown
    assert not (await load(ids["inactive"])).exists
    assert (await load(ids["superuser"])).is_superuser
    admin = await load(ids["admin"])
    assert admin.exists and admin.decide(USERS_VIEW).allowed
    plain = await load(ids["plain"])
    assert plain.exists and not plain.decide(USERS_VIEW).allowed


async def test_username_sync_real_db(
    container: AsyncContainer, clean_db: AsyncEngine, redis_client: Any
) -> None:
    ids = await _create_users(clean_db)
    await username_sync(container)(ids["admin"], "new_name")
    async with build_session_factory(clean_db)() as session:
        name = await session.scalar(
            select(IamUser.telegram_user_name).where(IamUser.telegram_user_id == ids["admin"])
        )
    assert name == "new_name"


async def test_click_log_reaches_db_and_files_without_text(
    clean_db: AsyncEngine, tmp_path: Path
) -> None:
    factory = build_session_factory(clean_db)
    buffer = LogBuffer(
        factory,
        DailyFileWriter(tmp_path),
        batch_size=10,
        flush_interval_seconds=0.05,
        prune_interval_seconds=None,
        counter=LogsDroppedCounter(),
    )
    bot_id = uuid4().int >> 66
    user = User(id=_tg(), is_bot=False, first_name="a", username="staff")
    chat = Chat(id=user.id, type="private")
    middleware = ClickLogMiddleware(buffer, telegram_bot_id=bot_id)

    async def handler(event: Any, data: dict[str, Any]) -> None:
        return None

    await buffer.start()
    for update_id, text in enumerate(("/start", f"мой токен {BOT_TOKEN}")):
        message = Message(
            message_id=update_id, date=datetime.now(UTC), chat=chat, from_user=user, text=text
        )
        update = Update(update_id=update_id, message=message)
        await middleware(handler, update, {"event_from_user": user, "event_chat": chat})
    await buffer.stop(10)

    async with factory() as session:
        rows = (
            await session.execute(
                select(UserActivity.action_type, UserActivity.button, UserActivity.tenant_id)
                .where(UserActivity.telegram_bot_id == bot_id)
                .order_by(UserActivity.action_type)
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        ("command", "/start", None),
        ("message", TEXT_MARKER, None),
    ]
    contents = _read_files(tmp_path)
    assert contents
    assert all(BOT_TOKEN not in text for text in contents)


def _read_files(root: Path) -> list[str]:
    return [path.read_text(encoding="utf-8") for path in root.rglob("*") if path.is_file()]
