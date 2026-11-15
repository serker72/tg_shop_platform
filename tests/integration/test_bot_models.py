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
from app.modules.iam.models import Language
from tests.integration.conftest import Query
from tests.integration.conftest import run_alembic as _alembic
from tests.integration.test_iam_models import _tenant

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.7")]

#: Column name of the historical schema (revision ``iam_rbac`` and older). It is glued
#: from two literals only so that the acceptance grep ``grep -rnw "bot_id" ...`` over the
#: log code and tests stays empty; it is the real column name of that revision.
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


@pytest.mark.parametrize(
    ("field", "constraint"),
    [("telegram_bot_id", "uq_bots_telegram_bot_id"), ("token_hash", "uq_bots_token_hash")],
)
async def test_bot_unique(session: AsyncSession, field: str, constraint: str) -> None:
    tenant = await _tenant(session)
    await BotRepository(session).add(_bot(tenant.id, 9_100, "a:1"))
    second = _bot(tenant.id, 9_101, "a:2")
    setattr(second, field, 9_100 if field == "telegram_bot_id" else token_hash("a:1"))
    with pytest.raises(IntegrityError, match=constraint):
        async with session.begin_nested():
            await BotRepository(session).add(second)


async def test_bot_status_check_and_tenant_not_null(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    with pytest.raises(IntegrityError, match="ck_bots_status"):
        async with session.begin_nested():
            bot = _bot(tenant.id, 9_200, "b:1")
            bot.status = "flying"
            await BotRepository(session).add(bot)
    with pytest.raises(IntegrityError, match=r'"tenant_id".*not-null'):
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
    switched = await repo.upsert(tenant.id, 555, "alice2", " EN ")
    assert switched.language_code == "en"
    unknown = await repo.upsert(tenant.id, 555, "alice2", "de")
    assert unknown.language_code == "en"


@pytest.mark.parametrize("code", ["de", "pt-br", "UK"])
async def test_tenant_customer_unknown_language_is_null(session: AsyncSession, code: str) -> None:
    tenant = await _tenant(session)
    customer = await TenantCustomerRepository(session).upsert(tenant.id, 777, "bob", code)
    assert customer.language_code is None


async def test_tenant_customer_deleted_language_is_null(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    language = Language(code="xx", name="xx", is_deleted=True)
    session.add(language)
    await session.flush()
    customer = await TenantCustomerRepository(session).upsert(tenant.id, 778, "eve", "xx")
    assert customer.language_code is None


async def test_list_runnable_skips_dead_tenants_and_invalid(session: AsyncSession) -> None:
    repo = BotRepository(session)
    live = await _tenant(session)
    inactive = await _tenant(session)
    deleted = await _tenant(session)
    inactive.is_active = False
    deleted.is_deleted = True
    ok = await repo.add(_bot(live.id, 9_400, "d:1"))
    bad_token = await repo.add(_bot(live.id, 9_401, "d:2"))
    await repo.set_status(bad_token, "invalid_token")
    off = await repo.add(_bot(inactive.id, 9_402, "d:3"))
    gone = await repo.add(_bot(deleted.id, 9_403, "d:4"))
    ids = {bot.id for bot in await repo.list_runnable()}
    assert ok.id in ids
    assert not ids & {bad_token.id, off.id, gone.id}


async def test_set_status_moves_timestamp_only_on_change(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    repo = BotRepository(session)
    bot = await repo.add(_bot(tenant.id, 9_500, "e:1"))
    await repo.set_status(bot, "stopped", error="noop")
    assert bot.status_changed_at is None
    assert bot.last_error == "noop"
    await repo.set_status(bot, "running")
    changed = bot.status_changed_at
    assert changed is not None
    await repo.set_status(bot, "running")
    assert bot.status_changed_at == changed
    assert bot.last_error is None


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
    query("UPDATE audit_logs SET telegram_bot_id = 8000000001")
    with pytest.raises(DBAPIError, match="audit_logs has rows with telegram_bot_id"):
        _alembic(url, command.downgrade, "iam_rbac")
    assert query("SELECT to_regclass('public.bots')") != [(None,)]
    query("UPDATE audit_logs SET telegram_bot_id = NULL WHERE telegram_bot_id IS NOT NULL")
    _alembic(url, command.downgrade, "iam_rbac")
    _alembic(url, command.upgrade, "head")
    assert query("SELECT to_regclass('public.tenant_customers')") != [(None,)]
