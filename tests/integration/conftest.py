"""Integration fixtures: infrastructure checks and PostgreSQL test database.

Fixture overview:

* ``migrated_db`` (session, sync) - the *test* database is reachable and migrated to ``head``.
* ``engine`` (function) - ``NullPool`` engine bound to the test loop.
* ``db_connection`` (function) - one connection with an outer transaction, rolled back.
* ``session`` / ``session_factory`` (function) - sessions joined into that outer
  transaction through savepoints: ``commit()`` in the code under test only releases a
  savepoint, everything disappears after the test.
* ``clean_db`` (function) - raw engine for tests that need a real commit, DDL or
  pg_partman maintenance; model tables are truncated after the test.
* ``scratch_db`` (function) - empty temporary ``<POSTGRES_DB>_mig_<hex8>_test`` database
  (no migrations), dropped with FORCE afterwards; ``migrated_scratch_db`` - the same at head.

Every database fixture depends on ``migrated_db``, which refuses non-test settings before
connecting and verifies ``current_database()`` after connecting (see ``_guards``).
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Table, make_url, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.migrations_support import import_all_models
from app.shared_kernel.infrastructure.database import Base, build_engine
from tests.integration._guards import (
    DatabaseTarget,
    assert_clean_db_compatible,
    assert_same_database,
    assert_test_database,
    assert_test_database_name,
    database_target,
    infra_unavailable,
)

__all__ = [
    "ALEMBIC_INI",
    "CLEAN_DB_PRESERVED_TABLES",
    "ROOT",
    "MigrationRun",
    "Query",
    "infra_unavailable",
    "reseed_languages_sql",
    "reseed_rbac_sql",
    "run_alembic",
]

#: Tables ``clean_db`` never truncates: Alembic bookkeeping and the ``system_settings``
#: singleton row inserted by the migration (TRUNCATE would delete it for good).
CLEAN_DB_PRESERVED_TABLES: frozenset[str] = frozenset({"alembic_version", "system_settings"})

ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = ROOT / "alembic.ini"

#: ``query(sql)`` on a scratch database (autocommit); returns fetched rows or ``[]``.
Query = Callable[..., list[tuple[Any, ...]]]


@dataclass(frozen=True, slots=True)
class MigrationRun:
    """Verified database and process state captured around ``alembic upgrade head``."""

    database: DatabaseTarget
    environ_before: dict[str, str]
    environ_after: dict[str, str]
    root_handlers_before: tuple[logging.Handler, ...]
    root_handlers_after: tuple[logging.Handler, ...]


def _connect_and_get_database(settings: Settings) -> str:
    """Open one sync connection and return ``current_database()``; no password in reasons."""
    pg = settings.postgres
    where = f"{pg.host}:{pg.port}/{pg.db}"
    if pg.password is None:
        infra_unavailable(f"POSTGRES_PASSWORD is not configured for {where}")
    try:
        with psycopg.connect(
            host=pg.host,
            port=pg.port,
            dbname=pg.db,
            user=pg.user,
            password=pg.password.get_secret_value(),
            connect_timeout=3,
        ) as connection:
            row = connection.execute("SELECT current_database()").fetchone()
    except psycopg.Error as error:
        detail = str(error).strip().splitlines()[0] if str(error).strip() else ""
        infra_unavailable(
            f"PostgreSQL is not reachable at {where} ({type(error).__name__}: {detail})"
        )
    if row is None:  # pragma: no cover - SELECT always returns a row
        pytest.fail("SELECT current_database() returned no row")
    return str(row[0])


@pytest.fixture(scope="session")
def migrated_db(settings: Settings) -> Iterator[MigrationRun]:
    """Ensure the *test* database is reachable and migrated to ``head``.

    Order: settings guard (no connection yet) -> connect -> ``current_database()`` guard
    (always fail) -> upgrade. The URL is handed to ``migrations/env.py`` through
    ``Config.attributes["sqlalchemy.url"]`` (in memory only, ``os.environ`` is untouched);
    ``configure_logger=False`` keeps env.py from reconfiguring pytest's logging.

    The upgrade runs in a worker thread: ``env.py`` calls ``asyncio.run()``, which fails
    inside a running loop and, in the main thread, also resets the current event loop that
    pytest-asyncio may rely on.

    There is no downgrade at teardown: the database belongs to the isolated test stack
    (``.env.test``, its own data dir). Consequently a revision edited *in place* is not
    re-applied (``alembic_version`` is already at head): recreate the schema with
    ``make test-integration-down PURGE=1`` and rerun ``make test-integration``.
    """
    assert_test_database(settings)
    assert_test_database_name(_connect_and_get_database(settings))

    config = Config(str(ALEMBIC_INI))
    config.attributes["sqlalchemy.url"] = settings.database_url
    config.attributes["configure_logger"] = False

    environ_before = dict(os.environ)
    handlers_before = tuple(logging.getLogger().handlers)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="alembic") as pool:
        pool.submit(command.upgrade, config, "head").result()
    yield MigrationRun(
        database=database_target(settings.database_url),
        environ_before=environ_before,
        environ_after=dict(os.environ),
        root_handlers_before=handlers_before,
        root_handlers_after=tuple(logging.getLogger().handlers),
    )


@pytest.fixture
async def engine(settings: Settings, migrated_db: MigrationRun) -> AsyncIterator[AsyncEngine]:
    """``NullPool`` engine per test.

    Function scope matches ``asyncio_default_fixture_loop_scope="function"``: connections
    are bound to the loop that opened them, so an engine shared across loops breaks.
    ``NullPool`` keeps no connection between tests.

    ``settings`` may be overridden by a module, so it is re-checked here: test settings
    and the very database ``migrated_db`` verified (host/port/db) - otherwise ``clean_db``
    could commit and TRUNCATE outside the test database.
    """
    assert_test_database(settings)
    engine = build_engine(settings, poolclass=NullPool)
    assert_same_database(migrated_db.database, settings.database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """One connection inside an outer transaction that is always rolled back."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            if transaction.is_active:
                await transaction.rollback()


@pytest.fixture
async def session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """Session joined into the outer transaction (SQLAlchemy "Joining a Session into an
    External Transaction").

    ``join_transaction_mode="create_savepoint"``: the session works inside a SAVEPOINT, so
    ``commit()`` / ``rollback()`` in the code under test never end the outer transaction;
    everything (including DDL - transactional in PostgreSQL) is rolled back afterwards.
    Not combinable with ``clean_db`` (see there).
    """
    session = AsyncSession(
        bind=db_connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
def session_factory(db_connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Factory of sessions sharing the outer transaction (and connection) of ``session``.

    All sessions run on ONE ``AsyncConnection``: use them strictly sequentially (open, work,
    close, then the next one). Never run them concurrently (``asyncio.gather``/tasks) - a
    connection cannot execute two statements at once.
    """
    return async_sessionmaker(
        bind=db_connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )


@pytest.fixture
async def clean_db(
    request: pytest.FixtureRequest, engine: AsyncEngine
) -> AsyncIterator[AsyncEngine]:
    """Engine with real commits; model tables are truncated after the test.

    Depends on ``migrated_db`` (through ``engine``), so the test-database guards apply.

    Incompatible with ``session`` / ``session_factory`` / ``db_connection``: their outer
    transaction is never committed and holds locks, so ``TRUNCATE`` would wait for it -
    the test fails immediately instead. As a second line of defence ``TRUNCATE`` runs with
    ``SET LOCAL lock_timeout = '5s'`` (``LockNotAvailable`` instead of a hang).

    ``TRUNCATE ... RESTART IDENTITY CASCADE`` covers every ``public`` table of
    ``Base.metadata`` except :data:`CLEAN_DB_PRESERVED_TABLES`; the ``system_settings``
    singleton is reset to its defaults instead (:func:`reset_system_settings_sql`), the
    ``languages`` reference rows are re-seeded (:func:`reseed_languages_sql`) and so are
    permissions, system roles and their links (:func:`reseed_rbac_sql`). On a
    partitioned parent PostgreSQL
    truncates all its partitions as well, so pg_partman children need no separate handling.
    Tables created by the test itself are the test's responsibility. No model tables ->
    no-op.
    """
    assert_clean_db_compatible(request.fixturenames)
    yield engine
    import_all_models()
    tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.schema in (None, "public") and table.name not in CLEAN_DB_PRESERVED_TABLES
    ]
    if not tables:
        return
    preparer = engine.dialect.identifier_preparer
    names = ", ".join(preparer.format_table(table) for table in tables)
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        await connection.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))
        await connection.execute(text(reset_system_settings_sql()))
        await connection.execute(text(reseed_languages_sql()))
        for statement in reseed_rbac_sql():
            await connection.execute(text(statement))


def reseed_languages_sql() -> str:
    """``INSERT`` restoring the ``languages`` reference rows seeded by the migration.

    ``languages`` is truncated by ``clean_db`` like any model table (so rows added or
    changed by a test never leak) and re-seeded from :data:`SEED_LANGUAGES` right after.
    """
    from app.modules.iam.models import SEED_LANGUAGES

    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    rows = ", ".join(
        f"(gen_random_uuid(), {literal(code)}, {literal(name)}, {str(default).lower()})"
        for code, name, default in SEED_LANGUAGES
    )
    return (
        "INSERT INTO languages (id, code, name, is_default) "  # noqa: S608
        f"VALUES {rows} ON CONFLICT (code) DO NOTHING"
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def reseed_rbac_sql() -> list[str]:
    """Idempotent ``INSERT``s restoring the seeded permissions, system roles and their
    links (:mod:`app.modules.iam.permissions`) after ``clean_db`` truncated them."""
    from app.modules.iam.permissions import (
        SEED_PERMISSIONS,
        SEED_ROLE_PERMISSIONS,
        SEED_ROLES,
    )

    permissions = ", ".join(
        f"(gen_random_uuid(), {_sql_literal(slug)}, {_sql_literal(name)})"
        for slug, name in SEED_PERMISSIONS
    )
    roles = ", ".join(
        f"(gen_random_uuid(), {_sql_literal(slug)}, {_sql_literal(name)}, true)"
        for slug, name in SEED_ROLES
    )
    links = ", ".join(
        f"({_sql_literal(role)}, {_sql_literal(permission)})"
        for role, slugs in SEED_ROLE_PERMISSIONS.items()
        for permission in sorted(slugs)
    )
    return [
        "INSERT INTO permissions (id, slug, name) "  # noqa: S608
        f"VALUES {permissions} ON CONFLICT (slug) DO NOTHING",
        "INSERT INTO roles (id, slug, name, is_system) "  # noqa: S608
        f"VALUES {roles} ON CONFLICT (slug) DO NOTHING",
        "INSERT INTO role_permissions (role_id, permission_id) "  # noqa: S608
        f"SELECT r.id, p.id FROM (VALUES {links}) AS v(role, permission) "
        "JOIN roles r ON r.slug = v.role JOIN permissions p ON p.slug = v.permission "
        "ON CONFLICT DO NOTHING",
    ]


def reset_system_settings_sql() -> str:
    """``UPDATE`` restoring every ``system_settings`` column except ``id`` / ``created_at``
    to its server default (``updated_at`` included), so values changed by one ``clean_db``
    test never leak into the next one. Built from ``SystemSettings.__table__``.
    """
    from app.modules.system.models import SINGLETON_ID, SystemSettings

    table = cast(Table, SystemSettings.__table__)
    columns = [c.name for c in table.columns if c.name not in {"id", "created_at"}]
    assignments = ", ".join(f'"{name}" = DEFAULT' for name in columns)
    return f"UPDATE system_settings SET {assignments} WHERE id = {SINGLETON_ID}"  # noqa: S608


def run_alembic(url: str, action: Callable[[Config, str], None], revision: str) -> None:
    """Run an Alembic command against ``url`` in a worker thread (see ``migrated_db``)."""
    config = Config(str(ALEMBIC_INI))
    config.attributes["sqlalchemy.url"] = url
    config.attributes["configure_logger"] = False
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="alembic") as pool:
        pool.submit(action, config, revision).result()


#: Keys owned by the permission cache - the only keys the fixture ever touches.
REDIS_TEST_KEY_PATTERN = "user:*"
#: Redis database reserved for tests; ``ops/scripts/env_test.sh`` writes ``REDIS_DB`` = 15.
REDIS_TEST_DB = 15


async def _drop_permission_keys(client: Any) -> None:
    from app.modules.iam.cache import GLOBAL_GENERATION_KEY

    keys = [key async for key in client.scan_iter(match=REDIS_TEST_KEY_PATTERN, count=500)]
    keys.append(GLOBAL_GENERATION_KEY.encode())
    if keys:
        await client.unlink(*keys)


@pytest.fixture
async def redis_client(settings: Settings) -> AsyncIterator[Any]:
    """``redis.asyncio.Redis`` of the test stack (:func:`build_redis`).

    Guards before any connection: ``PROJECT_ENVIRONMENT=test`` + ``*_test`` database (same
    rule as PostgreSQL) and ``REDIS_DB`` equal to the test database number
    (:data:`REDIS_TEST_DB`, written by ``ops/scripts/env_test.sh``) - the dev cache is never
    touched. Only the permission keys (``user:*``, ``perms:gen``) are removed, before and
    after the test; no ``FLUSHDB`` / ``FLUSHALL``.
    """
    from redis.exceptions import RedisError

    from app.shared_kernel.infrastructure.redis import build_redis

    assert_test_database(settings)
    if settings.redis.db != REDIS_TEST_DB:
        infra_unavailable(
            f"REDIS_DB={settings.redis.db} is not the test database {REDIS_TEST_DB}; "
            "refusing to touch a shared Redis database"
        )
    client = build_redis(settings)
    where = f"{settings.redis.host}:{settings.redis.port}/{settings.redis.db}"
    try:
        try:
            await client.ping()
        except (RedisError, OSError) as error:
            infra_unavailable(f"Redis is not reachable at {where} ({type(error).__name__})")
        await _drop_permission_keys(client)
        try:
            yield client
        finally:
            await _drop_permission_keys(client)
    finally:
        await client.aclose()


@pytest.fixture
def scratch_db(settings: Settings, migrated_db: MigrationRun) -> Iterator[tuple[str, Query]]:
    """Temporary ``<POSTGRES_DB>_mig_<hex8>_test`` database, dropped with FORCE afterwards."""
    assert_test_database(settings)
    pg = settings.postgres
    assert pg.password is not None
    name = f"{pg.db}_mig_{uuid.uuid4().hex[:8]}_test"
    assert_test_database_name(name)
    params: dict[str, Any] = {
        "host": pg.host,
        "port": pg.port,
        "user": pg.user,
        "password": pg.password.get_secret_value(),
        "autocommit": True,
    }
    with psycopg.connect(dbname=pg.db, **params) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_url(settings.database_url).set(database=name).render_as_string(False)

    def query(sql: str) -> list[tuple[Any, ...]]:
        with psycopg.connect(dbname=name, **params) as connection:
            cursor = connection.execute(sql)
            return list(cursor.fetchall()) if cursor.description else []

    try:
        yield url, query
    finally:
        with psycopg.connect(dbname=pg.db, **params) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def migrated_scratch_db(scratch_db: tuple[str, Query]) -> tuple[str, Query]:
    """``scratch_db`` upgraded to ``head``."""
    url, _ = scratch_db
    run_alembic(url, command.upgrade, "head")
    return scratch_db
