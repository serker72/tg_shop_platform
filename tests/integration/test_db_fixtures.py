"""Smoke tests of the PostgreSQL test fixtures (``tests/integration/conftest.py``).

Opt-in (``-m integration``); without a reachable test database the tests skip, with
``REQUIRE_INFRA=1`` they fail. Every test is order-independent.
"""

from __future__ import annotations

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from dishka import make_async_container
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.config import Settings
from app.shared_kernel.infrastructure.providers import DatabaseProvider
from tests.integration.conftest import ALEMBIC_INI, MigrationRun

pytestmark = pytest.mark.integration

# S608 below: table names are module constants (no user input); DDL/identifiers cannot be
# bound parameters, so f-strings are the only way to build these probe statements.


async def test_database_is_migrated_to_head(engine: AsyncEngine) -> None:
    head = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
    async with engine.connect() as connection:
        version = await connection.execute(text("SELECT version_num FROM alembic_version"))
        assert version.scalar_one() == head
        database = await connection.execute(text("SELECT current_database()"))
        assert database.scalar_one().endswith("_test")


def test_migration_leaves_environ_and_logging_untouched(migrated_db: MigrationRun) -> None:
    """A2/A3: the URL travels via ``Config.attributes``; env.py does not touch logging."""
    assert migrated_db.environ_after == migrated_db.environ_before
    assert migrated_db.root_handlers_after == migrated_db.root_handlers_before


async def test_session_commit_is_invisible_outside_outer_transaction(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    table = "fixture_savepoint_probe"
    await session.execute(text(f"CREATE TABLE {table} (id integer PRIMARY KEY)"))
    await session.execute(text(f"INSERT INTO {table} (id) VALUES (1)"))  # noqa: S608
    await session.commit()  # releases the savepoint only

    count = await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
    assert count.scalar_one() == 1
    async with engine.connect() as other:
        found = await other.execute(text(f"SELECT to_regclass('public.{table}')"))
        assert found.scalar_one() is None


async def test_savepoint_rollback_after_integrity_error_keeps_session_usable(
    session: AsyncSession,
) -> None:
    await session.execute(text("CREATE TEMP TABLE pk_probe (id integer PRIMARY KEY)"))
    await session.execute(text("INSERT INTO pk_probe (id) VALUES (1)"))
    await session.commit()

    with pytest.raises(IntegrityError):
        await session.execute(text("INSERT INTO pk_probe (id) VALUES (1)"))
    await session.rollback()

    await session.execute(text("INSERT INTO pk_probe (id) VALUES (2)"))
    rows = await session.execute(text("SELECT id FROM pk_probe ORDER BY id"))
    assert rows.scalars().all() == [1, 2]


async def test_session_factory_shares_outer_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as writer:
        await writer.execute(text("CREATE TEMP TABLE shared_probe (id integer)"))
        await writer.execute(text("INSERT INTO shared_probe VALUES (7)"))
        await writer.commit()
    async with session_factory() as reader:
        value = await reader.execute(text("SELECT id FROM shared_probe"))
        assert value.scalar_one() == 7


async def test_clean_db_commits_are_visible_to_new_connections(clean_db: AsyncEngine) -> None:
    table = "fixture_clean_db_probe"
    try:
        async with clean_db.begin() as connection:
            await connection.execute(text(f"CREATE TABLE {table} (id integer PRIMARY KEY)"))
            await connection.execute(text(f"INSERT INTO {table} (id) VALUES (1), (2)"))  # noqa: S608
        async with clean_db.connect() as connection:
            count = await connection.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            assert count.scalar_one() == 2
    finally:
        async with clean_db.begin() as connection:
            await connection.execute(text(f"DROP TABLE IF EXISTS {table}"))


async def test_truncate_of_partitioned_parent_clears_partitions(clean_db: AsyncEngine) -> None:
    """Backs the ``clean_db`` claim: TRUNCATE on a parent empties its partitions."""
    parent = "fixture_partitioned_probe"
    try:
        async with clean_db.begin() as connection:
            await connection.execute(
                text(f"CREATE TABLE {parent} (id integer, d date) PARTITION BY RANGE (d)")
            )
            await connection.execute(
                text(
                    f"CREATE TABLE {parent}_p20260101 PARTITION OF {parent} "
                    "FOR VALUES FROM ('2026-01-01') TO ('2026-02-01')"
                )
            )
            await connection.execute(text(f"INSERT INTO {parent} VALUES (1, '2026-01-15')"))  # noqa: S608
        async with clean_db.begin() as connection:
            await connection.execute(text(f"TRUNCATE TABLE {parent} RESTART IDENTITY CASCADE"))
        async with clean_db.connect() as connection:
            child = await connection.execute(text(f"SELECT count(*) FROM {parent}_p20260101"))  # noqa: S608
            assert child.scalar_one() == 0
    finally:
        async with clean_db.begin() as connection:
            await connection.execute(text(f"DROP TABLE IF EXISTS {parent}"))


async def test_dishka_request_session_discards_uncommitted_work(
    clean_db: AsyncEngine, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B7: REQUEST session without commit leaves nothing; container close disposes engine."""
    table = "fixture_dishka_probe"
    disposed: list[AsyncEngine] = []
    original_dispose = AsyncEngine.dispose

    async def spy_dispose(self: AsyncEngine, close: bool = True) -> None:
        disposed.append(self)
        await original_dispose(self, close)

    monkeypatch.setattr(AsyncEngine, "dispose", spy_dispose)
    async with clean_db.begin() as connection:
        await connection.execute(text(f"CREATE TABLE {table} (id integer PRIMARY KEY)"))
    container = make_async_container(DatabaseProvider(), context={Settings: settings})
    closed = False
    try:
        app_engine = await container.get(AsyncEngine)
        async with container() as request:
            db_session = await request.get(AsyncSession)
            await db_session.execute(text(f"INSERT INTO {table} (id) VALUES (1)"))  # noqa: S608
        async with clean_db.connect() as connection:
            count = await connection.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            assert count.scalar_one() == 0

        await container.close()
        closed = True
        assert app_engine in disposed
        assert isinstance(app_engine.pool, QueuePool)
        assert app_engine.pool.checkedout() == 0
    finally:
        if not closed:
            await container.close()
        async with clean_db.begin() as connection:
            await connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
