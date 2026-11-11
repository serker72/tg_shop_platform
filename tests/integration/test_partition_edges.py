"""Month boundaries and archive leftovers of the pg_partman tables (Stage 1 gaps).

Fixture rules: these tests insert rows and drop partitions, so they use the ``clean_db``
engine (real commits, TRUNCATE of the model tables afterwards) - never together with
``session`` (see :mod:`tests.integration.conftest` for the deadlock guard).
"""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.conftest import Query
from tests.integration.conftest import run_alembic as _alembic

pytestmark = pytest.mark.integration

TABLES = ("user_activity", "audit_logs")


def _insert_sql(table: str, moment_sql: str) -> str:
    """``INSERT`` of one row created at ``moment_sql``; returns the partition it landed in.

    The table name is chosen by this module (never by input) and ``moment_sql`` is an SQL
    literal written next to it, so the string built here is not user data.
    """
    if table == "user_activity":
        values = f"(gen_random_uuid(), {moment_sql}, gen_random_uuid(), 'click')"
        return (
            f"INSERT INTO user_activity (id, created_at, bot_id, action_type) VALUES {values} "  # noqa: S608
            "RETURNING tableoid::regclass::text"
        )
    values = f"(gen_random_uuid(), {moment_sql}, 'system', 'admin_action')"
    return (
        f"INSERT INTO audit_logs (id, created_at, actor_type, action) VALUES {values} "  # noqa: S608
        "RETURNING tableoid::regclass::text"
    )


async def _route(engine: AsyncEngine, table: str, moment_sql: str) -> str:
    async with engine.begin() as connection:
        statement = text(_insert_sql(table, moment_sql))  # noqa: S608 - see _insert_sql
        return str((await connection.execute(statement)).scalar_one())


async def _suffix(engine: AsyncEngine, month_offset_sql: str) -> str:
    async with engine.connect() as connection:
        value = (
            await connection.execute(
                text(f"SELECT to_char(date_trunc('month', now()) {month_offset_sql}, 'YYYYMMDD')")
            )
        ).scalar_one()
    return str(value)


@pytest.mark.parametrize("table", TABLES)
async def test_month_boundaries_route_explicitly(clean_db: AsyncEngine, table: str) -> None:
    """First second, a middle second and the last second of the month, then a far date.

    Boundaries are taken from the database: pg_partman cuts partitions in its own
    ``timezone``, not in Python's UTC.
    """
    current = await _suffix(clean_db, "")
    first = "date_trunc('month', now())"
    last = "date_trunc('month', now()) + interval '1 month' - interval '1 second'"
    assert await _route(clean_db, table, first) == f"{table}_p{current}"
    assert await _route(clean_db, table, f"{first} + interval '1 second'") == f"{table}_p{current}"
    assert await _route(clean_db, table, last) == f"{table}_p{current}"
    # One microsecond earlier belongs to the previous month (its own premade partition).
    previous = await _suffix(clean_db, "- interval '1 month'")
    assert await _route(clean_db, table, f"{first} - interval '1 microsecond'") == (
        f"{table}_p{previous}"
    )
    # Far outside the premade ring: the DEFAULT partition.
    assert await _route(clean_db, table, "now() + interval '5 years'") == f"{table}_default"


async def test_last_second_of_month_is_not_default(clean_db: AsyncEngine) -> None:
    """Explicit regression guard: the last second of the month never falls into DEFAULT."""
    current = await _suffix(clean_db, "")
    routed = await _route(
        clean_db,
        "user_activity",
        "date_trunc('month', now()) + interval '1 month' - interval '1 second'",
    )
    assert routed == f"user_activity_p{current}"
    async with clean_db.connect() as connection:
        # noqa: S608 - literal table name.
        default_rows = (
            await connection.execute(text("SELECT count(*) FROM public.user_activity_default"))
        ).scalar_one()
    assert default_rows == 0


def test_downgrade_leaves_no_archive_tables(migrated_scratch_db: tuple[str, Query]) -> None:
    """Decision R-9: the ``archive`` schema never outlives its partitioned parents.

    No hard-coded partition names: whatever retention moved into ``archive`` in *this*
    database is enumerated dynamically, then the revision is downgraded to ``base`` and the
    schema must be gone together with its parents. Rows still live in ``archive`` (a
    previous, unrelated run may have left some) are irrelevant - the downgrade drops the
    whole schema.
    """
    url, query = migrated_scratch_db
    parents = [
        str(row[0]) for row in query("SELECT parent_table FROM partman.part_config ORDER BY 1")
    ]
    assert parents, "partman.part_config is empty"
    table = parents[0].split(".", 1)[1]
    query(
        _insert_sql(table, "date_trunc('month', now()) - interval '3 months'").replace(
            " RETURNING tableoid::regclass::text", ""
        )
    )
    query(
        f"SELECT partman.drop_partition_time(p_parent_table := '{parents[0]}', "
        "p_retention := '1 month')"
    )
    archived = [
        str(row[0])
        for row in query(
            "SELECT relname FROM pg_class WHERE relnamespace = 'archive'::regnamespace "
            "AND relkind IN ('r', 'p') ORDER BY 1"
        )
    ]
    assert archived, "retention moved no partition to archive"

    _alembic(url, command.downgrade, "base")

    assert query("SELECT to_regnamespace('archive')") == [(None,)]
    assert query("SELECT to_regclass('partman.part_config')") == [(None,)]
    # Nothing survived: every archived relation is gone together with the schema
    # (``to_regclass`` returns NULL for an unknown schema instead of raising).
    for name in archived:
        assert query(f"SELECT to_regclass('archive.{name}')") == [(None,)]


def test_upgrade_refuses_a_non_empty_archive(migrated_scratch_db: tuple[str, Query]) -> None:
    """Decision R-9: a leftover ``archive`` blocks the upgrade with a clear message."""
    url, query = migrated_scratch_db
    _alembic(url, command.downgrade, "base")
    query("CREATE SCHEMA archive")
    query("CREATE TABLE archive.stale_rows (id integer)")

    with pytest.raises(Exception, match="schema archive is not empty"):
        _alembic(url, command.upgrade, "head")

    query("DROP SCHEMA archive CASCADE")
    _alembic(url, command.upgrade, "head")
    assert query("SELECT to_regclass('public.audit_logs')") != [(None,)]
