"""Partitioned logs (pg_partman 5.5.0), guard trigger, SystemSettings, migrations ring.

SQL is composed from module constants only (table names, partition names read from the
catalog) in a dozen places, hence the file-level S608 ignore instead of per-line noqa.
Month boundaries are always taken from the database (``date_trunc('month', now())``):
pg_partman cuts partitions in the database ``timezone``, not in Python's UTC.
"""

# ruff: noqa: S608

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.logging.models import AuditLog, UserActivity
from tests.integration.conftest import Query, reset_system_settings_sql
from tests.integration.conftest import run_alembic as _alembic

pytestmark = [pytest.mark.integration, pytest.mark.req("R7")]

TABLES = ("user_activity", "audit_logs")
INDEXES = {
    "user_activity": {
        "ix_user_activity_tenant_id_created_at",
        "ix_user_activity_telegram_user_id_created_at",
        "ix_user_activity_telegram_bot_id_created_at",
    },
    "audit_logs": {"ix_audit_logs_created_at", "ix_audit_logs_tenant_id_created_at"},
}
CHECK_VIOLATION = "23514"
#: The ring below exercises the ``partition_logs`` revision with data in both tables, so it
#: stops at the revision before ``bots``: ``bots`` refuses to convert a non-empty click-log
#: (no uuid -> bigint cast). Column / index names of that schema state.
RING_HEAD = "iam_rbac"
LEGACY_BOT_COLUMN = "bot" + "_id"
RING_INDEXES = {
    table: {name.replace("telegram_bot_id", LEGACY_BOT_COLUMN) for name in names}
    for table, names in INDEXES.items()
}


MONTH_SUFFIX_SQL = (
    "SELECT to_char(date_trunc('month', now()) + make_interval(months => g), 'YYYYMMDD') "
    "FROM generate_series(CAST(:a AS int), CAST(:b AS int)) g ORDER BY g"
)


async def _month_suffixes(session: AsyncSession, first: int, last: int) -> list[str]:
    rows = await session.execute(text(MONTH_SUFFIX_SQL), {"a": first, "b": last})
    return [str(value) for value in rows.scalars()]


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


# --- main test database (savepoint session) ------------------------------------------


@pytest.mark.req("S1")
async def test_part_config_and_premade_partitions(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT parent_table, control, partition_interval, premake, retention, "
                "retention_schema, retention_keep_table, retention_keep_index, "
                "infinite_time_partitions FROM partman.part_config ORDER BY parent_table"
            )
        )
    ).all()
    assert [tuple(row) for row in rows] == [
        ("public.audit_logs", "created_at", "1 mon", 3, "24 months", "archive", True, True, True),
        ("public.user_activity", "created_at", "1 mon", 3, "3 months", None, False, True, True),
    ]
    suffixes = await _month_suffixes(session, 0, 3)
    for table in TABLES:
        children = set(
            (
                await session.execute(
                    text(
                        "SELECT c.relname FROM pg_inherits i JOIN pg_class c "
                        "ON c.oid = i.inhrelid WHERE i.inhparent = CAST(:t AS regclass)"
                    ),
                    {"t": f"public.{table}"},
                )
            ).scalars()
        )
        expected = {f"{table}_p{suffix}" for suffix in suffixes}
        assert expected <= children
        assert f"{table}_default" in children


@pytest.mark.req("S2")
async def test_insert_routes_to_month_and_default(session: AsyncSession) -> None:
    last_moment = (
        await session.execute(
            text(
                "SELECT date_trunc('month', now()) + interval '1 month' - interval '1 microsecond'"
            )
        )
    ).scalar_one()
    far = datetime.now(UTC) + timedelta(days=5 * 366)
    (current,) = await _month_suffixes(session, 0, 0)
    routed = []
    for moment in (last_moment, far):
        row = UserActivity(
            telegram_bot_id=uuid.uuid4().int >> 66, action_type="click", created_at=moment
        )
        session.add(row)
        await session.flush()
        routed.append(
            (
                await session.execute(
                    text("SELECT tableoid::regclass::text FROM user_activity WHERE id = :id"),
                    {"id": row.id},
                )
            ).scalar_one()
        )
    assert routed == [f"user_activity_p{current}", "user_activity_default"]


async def test_orm_roundtrip_and_composite_pk(session: AsyncSession) -> None:
    activity = UserActivity(
        telegram_bot_id=uuid.uuid4().int >> 66,
        action_type="open_product",
        callback_data="product:1",
        metadata_={"k": "v"},
    )
    audit = AuditLog(actor_type="system", action="settings_change", payload={"a": 1})
    session.add_all([activity, audit])
    await session.flush()
    session.expunge_all()
    loaded = await session.get(UserActivity, (activity.id, activity.created_at))
    assert loaded is not None and loaded.metadata_ == {"k": "v"}
    raw = (
        await session.execute(
            text("SELECT metadata FROM user_activity WHERE id = :id"), {"id": activity.id}
        )
    ).scalar_one()
    assert raw == {"k": "v"}
    assert (
        await session.execute(select(AuditLog.action).where(AuditLog.id == audit.id))
    ).scalar_one() == "settings_change"


async def test_audit_actor_type_check(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(AuditLog(actor_type="alien", action="admin_action"))
            await session.flush()


@pytest.mark.parametrize(
    "assignment",
    [
        "retention_keep_table = false",
        "retention_keep_table = NULL",
        "retention_schema = NULL",
        "retention = NULL",
        "parent_table = 'public.x'",
        "parent_table = 'public.x', retention_keep_table = false",
    ],
)
@pytest.mark.req("S3")
async def test_guard_blocks_audit_config(session: AsyncSession, assignment: str) -> None:
    with pytest.raises(DBAPIError) as caught:
        async with session.begin_nested():
            await session.execute(
                text(
                    f"UPDATE partman.part_config SET {assignment} "
                    "WHERE parent_table = 'public.audit_logs'"
                )
            )
    assert _sqlstate(caught.value) == CHECK_VIOLATION
    assert "PARTMAN_RETENTION_GUARD" in str(caught.value.orig)
    assert (await session.execute(text("SELECT 1"))).scalar_one() == 1


@pytest.mark.req("S3")
async def test_guard_allows_user_activity(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "UPDATE partman.part_config SET retention_keep_table = false "
            "WHERE parent_table = 'public.user_activity'"
        )
    )
    assert result.rowcount == 1  # type: ignore[attr-defined]


@pytest.mark.req("S3")
async def test_guard_function_hardening(session: AsyncSession) -> None:
    config, public_can_execute = (
        await session.execute(
            text(
                "SELECT proconfig, has_function_privilege('public', p.oid, 'EXECUTE') "
                "FROM pg_proc p WHERE p.proname = 'tg_guard_audit_part_config'"
            )
        )
    ).one()
    assert config == ["search_path=pg_catalog, pg_temp"]
    assert public_can_execute is False
    # pg_partman's own UPDATE of part_config passes the trigger.
    await session.execute(text("SELECT partman.run_maintenance()"))


async def test_system_settings_singleton(session: AsyncSession) -> None:
    row = (
        await session.execute(
            text(
                "SELECT id, user_activity_retention_months, audit_logs_retention_months "
                "FROM system_settings"
            )
        )
    ).all()
    assert [tuple(r) for r in row] == [(1, 3, 24)]
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(text("INSERT INTO system_settings (id) VALUES (2)"))
    for value in (0, 121):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(
                    text("UPDATE system_settings SET audit_logs_retention_months = :v"),
                    {"v": value},
                )


async def test_reset_system_settings_restores_defaults(session: AsyncSession) -> None:
    """The statement ``clean_db`` runs after TRUNCATE resets every changed value."""
    await session.execute(
        text(
            "UPDATE system_settings SET is_maintenance_mode = true, iam_cache_ttl_seconds = 5, "
            "extra_config = '{\"x\": 1}', user_activity_retention_months = 7, "
            "audit_logs_retention_months = 30, updated_at = now() - interval '1 day'"
        )
    )
    await session.execute(text(reset_system_settings_sql()))
    row = (
        await session.execute(
            text(
                "SELECT is_maintenance_mode, iam_cache_ttl_seconds, extra_config, "
                "user_activity_retention_months, audit_logs_retention_months, "
                "updated_at = now() FROM system_settings"
            )
        )
    ).one()
    assert tuple(row) == (False, 86400, {}, 3, 24, True)


async def test_clean_db_keeps_system_settings_row(session: AsyncSession) -> None:
    """``clean_db`` excludes ``system_settings`` (see ``CLEAN_DB_PRESERVED_TABLES``)."""
    from tests.integration.conftest import CLEAN_DB_PRESERVED_TABLES

    assert "system_settings" in CLEAN_DB_PRESERVED_TABLES
    count = (await session.execute(text("SELECT count(*) FROM system_settings"))).scalar_one()
    assert count == 1


# --- scratch database: upgrade / downgrade ring ----------------------------------------


# ``scratch_db`` / ``Query`` / ``run_alembic`` live in ``tests/integration/conftest.py``.


def _relkinds(query: Query) -> dict[str, str]:
    rows = query(
        "SELECT relname, relkind::text FROM pg_class WHERE relnamespace = 'public'::regnamespace "
        "AND relname IN ('user_activity', 'audit_logs')"
    )
    return {str(r[0]): str(r[1]) for r in rows}


def _counts(query: Query) -> dict[str, int]:
    return {t: int(query(f"SELECT count(*) FROM public.{t}")[0][0]) for t in TABLES}


def _children(query: Query, table: str) -> list[str]:
    rows = query(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        f"WHERE i.inhparent = 'public.{table}'::regclass ORDER BY 1"
    )
    return [str(r[0]) for r in rows]


def _seed(query: Query) -> None:
    moments = (
        "now()",
        "now() - interval '1 month'",
        "now() + interval '5 years'",
        "now() - interval '18 months'",
    )
    for moment in moments:
        query(
            f"INSERT INTO user_activity (id, created_at, {LEGACY_BOT_COLUMN}, action_type) "
            f"VALUES (gen_random_uuid(), {moment}, gen_random_uuid(), 'click')"
        )
        query(
            "INSERT INTO audit_logs (id, created_at, actor_type, action) "
            f"VALUES (gen_random_uuid(), {moment}, 'system', 'admin_action')"
        )


def test_upgrade_downgrade_ring(scratch_db: tuple[str, Query]) -> None:
    url, query = scratch_db
    _alembic(url, command.upgrade, RING_HEAD)
    assert _relkinds(query) == {"user_activity": "p", "audit_logs": "p"}
    assert query("SELECT count(*) FROM pg_class WHERE relnamespace = 'archive'::regnamespace") == [
        (0,)
    ]
    assert query("SELECT id FROM system_settings") == [(1,)]
    _seed(query)
    assert _counts(query) == {"user_activity": 4, "audit_logs": 4}

    _alembic(url, command.downgrade, "system_settings")
    assert _relkinds(query) == {"user_activity": "r", "audit_logs": "r"}
    assert _counts(query) == {"user_activity": 4, "audit_logs": 4}
    for table in TABLES:
        pk = query(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            f"WHERE conrelid = 'public.{table}'::regclass AND contype = 'p'"
        )
        assert pk == [(f"pk_{table}", "PRIMARY KEY (id, created_at)")]
        names = {
            r[0] for r in query(f"SELECT indexname FROM pg_indexes WHERE tablename = '{table}'")
        }
        assert names == RING_INDEXES[table] | {f"pk_{table}"}
        leftovers = query(
            "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
            f"AND (relname LIKE '{table}\\_p%' OR relname LIKE '{table}\\_default' "
            f"OR relname LIKE '{table}\\_undo%' OR relname LIKE '{table}\\_legacy%')"
        )
        assert leftovers == []
    assert query("SELECT 1 FROM pg_proc WHERE proname = 'tg_guard_audit_part_config'") == []
    assert query("SELECT 1 FROM pg_extension WHERE extname = 'pg_partman'") == []
    assert query("SELECT 1 FROM pg_namespace WHERE nspname IN ('partman', 'archive')") == []

    _alembic(url, command.upgrade, RING_HEAD)
    assert _relkinds(query) == {"user_activity": "p", "audit_logs": "p"}
    assert _counts(query) == {"user_activity": 4, "audit_logs": 4}
    assert query("SELECT count(*) FROM pg_trigger WHERE tgname = 'guard_audit_part_config'") == [
        (1,)
    ]
    old_suffix = query(
        "SELECT to_char(date_trunc('month', now() - interval '18 months'), 'YYYYMMDD')"
    )
    for table in TABLES:
        # Conversion: nothing left in DEFAULT, the old row sits in its own month partition.
        assert query(f"SELECT count(*) FROM public.{table}_default") == [(0,)]
        assert query(
            f"SELECT tableoid::regclass::text FROM public.{table} "
            "WHERE created_at < now() - interval '17 months'"
        ) == [(f"{table}_p{old_suffix[0][0]}",)]
    query("SELECT partman.run_maintenance()")
    for table in TABLES:
        assert query(f"SELECT count(*) FROM public.{table}_default") == [(0,)]

    # retention (3 months) dropped the 18-month-old click-log partition; audit kept it.
    assert _counts(query) == {"user_activity": 3, "audit_logs": 4}

    # --- guard contract: `downgrade` leaves no orphan, `upgrade` refuses leftovers ------
    _alembic(url, command.downgrade, "base")
    assert query("SELECT to_regclass('public.system_settings')") == [(None,)]
    assert query("SELECT to_regnamespace('archive')") == [(None,)]
    # Reproduce the state an older downgrade could leave behind: non-empty ``archive``.
    query("CREATE SCHEMA archive")
    query(
        "CREATE TABLE archive.audit_logs_p20200101 "
        "(id uuid NOT NULL, created_at timestamptz NOT NULL, actor_type text, action text)"
    )
    with pytest.raises(DBAPIError, match="schema archive is not empty"):
        _alembic(url, command.upgrade, RING_HEAD)
    # The guard is non-destructive: the operator decides what to do with the leftovers.
    assert query("SELECT to_regclass('archive.audit_logs_p20200101')") != [(None,)]

    # --- retention detaches partitions -> downgrade drops archive -> upgrade passes ----
    query("DROP SCHEMA archive CASCADE")
    _alembic(url, command.upgrade, RING_HEAD)
    assert _relkinds(query) == {"user_activity": "p", "audit_logs": "p"}
    assert _counts(query) == {"user_activity": 3, "audit_logs": 4}
    # A row in a premade month partition, then retention with a short window: partman
    # detaches the old audit partitions into ``archive`` (retention_keep_table).
    query(
        "INSERT INTO audit_logs (id, created_at, actor_type, action) VALUES "
        "(gen_random_uuid(), date_trunc('month', now()) - interval '3 months', "
        "'system', 'admin_action')"
    )
    query("SELECT partman.drop_partition_time('public.audit_logs', p_retention := '1 month')")
    kept = _counts(query)
    assert kept["audit_logs"] < 5, "retention detached nothing"
    detached = [
        str(row[0])
        for row in query(
            "SELECT relname FROM pg_class WHERE relnamespace = 'archive'::regnamespace "
            "AND relkind = 'r' ORDER BY 1"
        )
    ]
    assert detached, "retention moved nothing to archive"
    _alembic(url, command.downgrade, "system_settings")
    # Archive went with the parent: no orphan is left for the next upgrade to trip over.
    assert query("SELECT to_regnamespace('archive')") == [(None,)]
    for name in detached:
        assert query(f"SELECT to_regclass('archive.{name}')") == [(None,)]
    # Rows still attached to the parent survive the ring; only the archive is gone.
    assert _counts(query) == kept
    _alembic(url, command.upgrade, RING_HEAD)
    assert _relkinds(query) == {"user_activity": "p", "audit_logs": "p"}
    assert _counts(query) == kept


def test_maintenance_premakes_without_recent_rows(
    migrated_scratch_db: tuple[str, Query],
) -> None:
    """infinite_time_partitions=true: future partitions appear although the newest row is
    3 months old (with ``false`` pg_partman premakes from the newest non-empty child)."""
    _, query = migrated_scratch_db
    suffixes = [
        str(r[0])
        for r in query(
            "SELECT to_char(date_trunc('month', now()) + make_interval(months => g), "
            "'YYYYMMDD') FROM generate_series(-3, 3) g ORDER BY g"
        )
    ]
    for table in TABLES:
        if table == "user_activity":
            query(
                "INSERT INTO user_activity (id, created_at, telegram_bot_id, action_type) VALUES "
                "(gen_random_uuid(), date_trunc('month', now()) - interval '3 months', "
                "1, 'click')"
            )
        for suffix in suffixes[1:]:
            query(f"DROP TABLE public.{table}_p{suffix}")
        assert f"{table}_p{suffixes[-1]}" not in _children(query, table)
        query(f"SELECT partman.run_maintenance('public.{table}')")
        children = _children(query, table)
        assert {f"{table}_p{suffix}" for suffix in suffixes[3:]} <= set(children)


def test_downgrade_drops_archive_schema(migrated_scratch_db: tuple[str, Query]) -> None:
    """Decision R-9: a detached partition never outlives its parent."""
    url, query = migrated_scratch_db
    query(
        "INSERT INTO audit_logs (id, created_at, actor_type, action) VALUES "
        "(gen_random_uuid(), date_trunc('month', now()) - interval '3 months', 'system', 'x')"
    )
    # Real retention path: detach + move to ``archive`` (retention_schema from part_config).
    query("SELECT partman.drop_partition_time('public.audit_logs', p_retention := '1 month')")
    archived = query(
        "SELECT relname FROM pg_class WHERE relnamespace = 'archive'::regnamespace "
        "AND relkind = 'r' ORDER BY 1"
    )
    assert archived, "drop_partition_time moved nothing to archive"
    rows_in_archive = sum(
        int(query(f"SELECT count(*) FROM archive.{r[0]}")[0][0]) for r in archived
    )
    assert rows_in_archive == 1

    _alembic(url, command.downgrade, "system_settings")
    # The archive schema and the detached partitions it held are gone with the parents.
    assert query("SELECT to_regnamespace('archive')") == [(None,)]
    assert _counts(query) == {"user_activity": 0, "audit_logs": 0}
    _alembic(url, command.upgrade, "head")
    assert _relkinds(query) == {"user_activity": "p", "audit_logs": "p"}
    # A second ring is possible: ``upgrade`` re-creates an empty ``archive``.
    assert query("SELECT to_regnamespace('archive')") != [(None,)]


def _migration_module() -> Any:
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "20261106_1210-partition_logs_partition_logs.py"
    )
    spec = importlib.util.spec_from_file_location("partition_logs_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unpartition_template_guard(migrated_scratch_db: tuple[str, Query]) -> None:
    """The guard of ``_unpartition`` fires while the template of *this* table exists.

    Runs before the ``undo_partition`` loop, so the ``part_config`` row still names the
    template. Another table's template and a table without ``part_config`` never trip it.
    """
    _, query = migrated_scratch_db
    guard = _migration_module()._template_guard_sql
    with pytest.raises(psycopg.errors.RaiseException, match="partman template still exists"):
        query(guard("audit_logs"))
    # No ``part_config`` row for this name: nothing to check.
    query(guard("no_such_parent"))
    # Template of audit_logs removed: its guard passes, user_activity's template is untouched
    # and still detected (exact comparison, not a suffix ``LIKE``).
    query("DROP TABLE partman.template_public_audit_logs")
    query(guard("audit_logs"))
    with pytest.raises(psycopg.errors.RaiseException, match="partman template still exists"):
        query(guard("user_activity"))


def test_downgrade_fails_on_foreign_object_in_partman(
    migrated_scratch_db: tuple[str, Query],
) -> None:
    url, query = migrated_scratch_db
    templates = query("SELECT template_table FROM partman.part_config ORDER BY 1")
    assert templates == [
        ("partman.template_public_audit_logs",),
        ("partman.template_public_user_activity",),
    ]
    query("CREATE TABLE partman.foreign_object (id int)")
    with pytest.raises(DBAPIError):
        _alembic(url, command.downgrade, "system_settings")
    # The whole downgrade was rolled back: still partitioned, foreign object intact.
    assert _relkinds(query) == {"user_activity": "p", "audit_logs": "p"}
    assert query("SELECT to_regclass('partman.foreign_object')::text") == [
        ("partman.foreign_object",)
    ]
    query("DROP TABLE partman.foreign_object")
    _alembic(url, command.downgrade, "system_settings")
    assert query("SELECT 1 FROM pg_namespace WHERE nspname = 'partman'") == []
