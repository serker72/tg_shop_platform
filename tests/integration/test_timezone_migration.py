import uuid

import pytest
from alembic import command
from sqlalchemy import insert, select, update
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bot_manager.models import Customer
from app.modules.iam.models import Tenant, User
from tests.integration.conftest import Query, run_alembic

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.17")]

PREVIOUS = "20261118_1210"
REVISION = "20261118_1220"
COLUMNS = """
SELECT table_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND column_name = 'timezone'
ORDER BY table_name
"""
ROWS = """
SELECT t.timezone, u.timezone, c.timezone
FROM tenants t JOIN customers c ON t.id = c.tenant_id
JOIN users u ON u.telegram_user_id = c.telegram_user_id
WHERE t.slug = 'timezone-test'
"""
OLD_DATA = """
SELECT t.id, t.name, t.slug, t.created_at, t.updated_at,
       u.id, u.telegram_user_name, u.created_at, u.updated_at,
       c.id, c.username, c.created_at, c.updated_at
FROM tenants t JOIN customers c ON t.id = c.tenant_id
JOIN users u ON u.telegram_user_id = c.telegram_user_id
WHERE t.slug = 'timezone-test'
"""
EVENTS = """
SELECT id, created_at, action FROM audit_logs
WHERE id = '00000000-0000-0000-0000-000000000001'
UNION ALL
SELECT id, created_at, action_type FROM user_activity
WHERE id = '00000000-0000-0000-0000-000000000002'
ORDER BY id
"""


def _seed_existing_rows(query: Query) -> None:
    query("""
        INSERT INTO tenants (id, name, slug, created_at, updated_at)
        VALUES (gen_random_uuid(), 'Test', 'timezone-test',
                '2026-01-01 23:59:58.123456-05', '2026-01-02 01:02:03-05')
    """)
    query("""
        INSERT INTO users (id, telegram_user_id, telegram_user_name, created_at, updated_at)
        VALUES (gen_random_uuid(), 42, 'old_staff',
                '2026-01-01 00:01:02+14', '2026-01-02 04:05:06+14')
    """)
    query("""
        INSERT INTO customers (id, tenant_id, telegram_user_id, username, created_at, updated_at)
        SELECT gen_random_uuid(), id, 42, 'old_customer',
               TIMESTAMPTZ '2026-01-01 00:01:02-10', TIMESTAMPTZ '2026-01-02 04:05:06-10'
        FROM tenants WHERE slug = 'timezone-test'
    """)
    query("""
        INSERT INTO audit_logs (id, created_at, actor_type, action)
        VALUES ('00000000-0000-0000-0000-000000000001',
                '2026-01-02 01:02:03.000004+05:45', 'system', 'timezone_audit_seed')
    """)
    query("""
        INSERT INTO user_activity (id, created_at, telegram_bot_id, action_type)
        VALUES ('00000000-0000-0000-0000-000000000002',
                '2026-01-01 23:59:58.123456-10', 42, 'timezone_activity_seed')
    """)


@pytest.mark.parametrize("partial_data", [False, True])
def test_timezone_migration_ring(
    scratch_db: tuple[str, Query], monkeypatch: pytest.MonkeyPatch, partial_data: bool
) -> None:
    url, query = scratch_db
    monkeypatch.setenv("DEFAULT_TIMEZONE", "Asia/Tokyo")
    run_alembic(url, command.upgrade, PREVIOUS)
    assert query(COLUMNS) == []
    if partial_data:
        _seed_existing_rows(query)
    old_data, old_events = query(OLD_DATA), query(EVENTS)
    assert len(old_data) == int(partial_data)
    assert len(old_events) == 2 * int(partial_data)

    run_alembic(url, command.upgrade, REVISION)
    assert query(COLUMNS) == [
        ("customers", "text", "YES", None),
        ("tenants", "text", "NO", "'UTC'::text"),
        ("users", "text", "YES", None),
    ]
    assert query(ROWS) == ([("UTC", None, None)] if partial_data else [])
    assert query("""
        INSERT INTO tenants (id, name, slug) VALUES (gen_random_uuid(), 'New', 'new-timezone-test')
        RETURNING timezone
    """) == [("UTC",)]
    assert query(OLD_DATA) == old_data and query(EVENTS) == old_events
    query("UPDATE tenants SET timezone = 'Europe/Riga' WHERE slug = 'timezone-test'")
    run_alembic(url, command.downgrade, PREVIOUS)
    assert query(COLUMNS) == []
    assert query(OLD_DATA) == old_data and query(EVENTS) == old_events
    run_alembic(url, command.upgrade, REVISION)
    assert query(ROWS) == ([("UTC", None, None)] if partial_data else [])
    assert query(OLD_DATA) == old_data and query(EVENTS) == old_events
    assert query("""
        SELECT table_name FROM information_schema.columns
        WHERE table_schema = 'public' AND column_name = 'created_at'
          AND data_type = 'timestamp with time zone'
          AND table_name IN ('audit_logs', 'user_activity')
        ORDER BY table_name
    """) == [("audit_logs",), ("user_activity",)]


async def test_typed_bulk_dml_validates_timezone_on_all_models(session: AsyncSession) -> None:
    tenant_id = uuid.uuid4()
    for model, values in (
        (Tenant, {"id": tenant_id, "name": "Test", "slug": "timezone-bulk"}),
        (User, {"id": uuid.uuid4(), "telegram_user_id": 1234567}),
        (Customer, {"id": uuid.uuid4(), "tenant_id": tenant_id, "telegram_user_id": 1234567}),
    ):
        with pytest.raises(StatementError, match="Invalid IANA timezone ID"):
            await session.execute(insert(model), [{**values, "timezone": "+02:00"}])
        await session.execute(insert(model), [{**values, "timezone": "Europe/Riga"}])
        target = model.id == values["id"]
        with pytest.raises(StatementError, match="Invalid IANA timezone ID"):
            await session.execute(update(model).where(target).values(timezone="Mars/Olympus"))
        assert await session.scalar(select(model.timezone).where(target)) == "Europe/Riga"
        await session.execute(update(model).where(target).values(timezone="UTC"))
        assert await session.scalar(select(model.timezone).where(target)) == "UTC"
