"""Bots / tenant_customers against the migrated test database; ``bots`` revision."""

# ruff: noqa: S608

from __future__ import annotations

import uuid

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bot_manager.models import Bot
from app.modules.bot_manager.repository import BotRepository, TenantCustomerRepository
from app.modules.bot_manager.security import token_hash
from tests.integration.conftest import Query
from tests.integration.conftest import run_alembic as _alembic
from tests.integration.test_iam_models import _tenant

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.7")]

LEGACY_BOT_COLUMN = "bot" + "_id"


def _bot(tenant_id: uuid.UUID, telegram_bot_id: int, token: str) -> Bot:
    return Bot(
        tenant_id=tenant_id,
        telegram_bot_id=telegram_bot_id,
        encrypted_token=token,
        token_hash=token_hash(token),
    )


async def test_token_encrypted_roundtrip(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    token = "111:plain-token"  # noqa: S105
    bot = await BotRepository(session).add(_bot(tenant.id, 9_001, token))
    assert bot.status == "stopped"
    assert bot.configuration == {}
    raw = await session.scalar(
        text("SELECT encrypted_token FROM bots WHERE id = :i"), {"i": bot.id}
    )
    assert raw != token
    assert token not in str(raw)
    session.expunge_all()
    loaded = await BotRepository(session).by_token_hash(token_hash(token), for_update=True)
    assert loaded is not None
    assert loaded.encrypted_token == token
    assert (await BotRepository(session).by_telegram_bot_id(9_001)) is not None
    assert await BotRepository(session).list_for_tenants([tenant.id]) == [loaded]
    await BotRepository(session).set_status(loaded, "error", error="boom")
    assert loaded.status_changed_at is not None


@pytest.mark.parametrize("field", ["telegram_bot_id", "token_hash"])
async def test_bot_unique(session: AsyncSession, field: str) -> None:
    tenant = await _tenant(session)
    await BotRepository(session).add(_bot(tenant.id, 9_100, "a:1"))
    second = _bot(tenant.id, 9_101, "a:2")
    setattr(second, field, 9_100 if field == "telegram_bot_id" else token_hash("a:1"))
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await BotRepository(session).add(second)


async def test_bot_status_check_and_tenant_not_null(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    with pytest.raises(IntegrityError, match="ck_bots_status"):
        async with session.begin_nested():
            bot = _bot(tenant.id, 9_200, "b:1")
            bot.status = "flying"
            await BotRepository(session).add(bot)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await BotRepository(session).add(_bot(None, 9_201, "b:2"))  # type: ignore[arg-type]


async def test_tenant_customer_upsert(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    bot = await BotRepository(session).add(_bot(tenant.id, 9_300, "c:1"))
    repo = TenantCustomerRepository(session)
    first = await repo.upsert(tenant.id, 555, "alice", "ru", first_bot_id=bot.id)
    again = await repo.upsert(tenant.id, 555, "alice2", None, first_bot_id=None)
    assert again.id == first.id
    assert (again.username, again.language_code, again.first_bot_id) == ("alice2", "ru", bot.id)


def _column_type(query: Query, table: str, column: str) -> list[tuple[object, ...]]:
    return query(
        "SELECT format_type(atttypid, atttypmod), attnotnull FROM pg_attribute "
        f"WHERE attrelid = to_regclass('{table}') AND attname = '{column}' AND NOT attisdropped"
    )


def test_bots_revision_columns_templates_and_new_partition(
    migrated_scratch_db: tuple[str, Query],
) -> None:
    _, query = migrated_scratch_db
    assert _column_type(query, "public.user_activity", "telegram_bot_id") == [("bigint", True)]
    assert _column_type(query, "public.audit_logs", "telegram_bot_id") == [("bigint", False)]
    templates = [str(r[0]) for r in query("SELECT template_table FROM partman.part_config")]
    assert templates
    for template in templates:
        if "user_activity" in template or "audit_logs" in template:
            assert _column_type(query, template, LEGACY_BOT_COLUMN) == []
            assert [r[0] for r in _column_type(query, template, "telegram_bot_id")] == ["bigint"]
    assert query(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'ix_user_activity_telegram_bot_id_created_at'"
    ) == [(1,)]
    # A partition created by pg_partman after the upgrade (2 years ahead, beyond premake).
    suffix = query("SELECT to_char(date_trunc('month', now()) + interval '24 months', 'YYYYMMDD')")
    for table in ("user_activity", "audit_logs"):
        query(
            f"SELECT partman.create_partition_time('public.{table}', "
            "ARRAY[date_trunc('month', now()) + interval '24 months'])"
        )
        child = f"public.{table}_p{suffix[0][0]}"
        assert [r[0] for r in _column_type(query, child, "telegram_bot_id")] == ["bigint"]
        assert _column_type(query, child, LEGACY_BOT_COLUMN) == []
    query(
        "INSERT INTO user_activity (id, created_at, telegram_bot_id, action_type) VALUES "
        "(gen_random_uuid(), now() + interval '24 months', 8000000000, 'click')"
    )


def test_bots_revision_guard_and_ring(migrated_scratch_db: tuple[str, Query]) -> None:
    url, query = migrated_scratch_db
    _alembic(url, command.downgrade, "iam_rbac")
    assert query("SELECT to_regclass('public.bots')") == [(None,)]
    assert _column_type(query, "public.user_activity", LEGACY_BOT_COLUMN) == [("uuid", True)]
    assert _column_type(query, "public.audit_logs", LEGACY_BOT_COLUMN) == [("uuid", False)]
    query(
        f"INSERT INTO user_activity (id, created_at, {LEGACY_BOT_COLUMN}, action_type) "
        "VALUES (gen_random_uuid(), now(), gen_random_uuid(), 'click')"
    )
    with pytest.raises(DBAPIError, match="user_activity is not empty"):
        _alembic(url, command.upgrade, "head")
    assert query("SELECT to_regclass('public.bots')") == [(None,)]
    query("TRUNCATE public.user_activity")
    query(
        f"INSERT INTO audit_logs (id, created_at, {LEGACY_BOT_COLUMN}, actor_type, action) "
        "VALUES (gen_random_uuid(), now(), gen_random_uuid(), 'system', 'x')"
    )
    with pytest.raises(DBAPIError, match="audit_logs has rows"):
        _alembic(url, command.upgrade, "head")
    query(f"UPDATE audit_logs SET {LEGACY_BOT_COLUMN} = NULL")
    _alembic(url, command.upgrade, "head")
    assert _column_type(query, "public.user_activity", "telegram_bot_id") == [("bigint", True)]
    assert query("SELECT count(*) FROM audit_logs") == [(1,)]
    query(
        "INSERT INTO user_activity (id, created_at, telegram_bot_id, action_type) "
        "VALUES (gen_random_uuid(), now(), 1, 'click')"
    )
    with pytest.raises(DBAPIError, match="user_activity is not empty"):
        _alembic(url, command.downgrade, "iam_rbac")
    query("TRUNCATE public.user_activity")
    _alembic(url, command.downgrade, "iam_rbac")
    _alembic(url, command.upgrade, "head")
    assert query("SELECT to_regclass('public.tenant_customers')") != [(None,)]
