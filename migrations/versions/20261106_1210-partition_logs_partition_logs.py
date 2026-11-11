"""partition_logs

Partitioned ``user_activity`` / ``audit_logs`` (pg_partman 5.5.0, monthly, premake 3,
DEFAULT partition) + guard trigger on ``partman.part_config`` for ``public.audit_logs``.

Upgrade
    * refuses to run on a non-empty ``archive`` schema - a leftover of an older downgrade
      must be imported or dropped by the operator, never adopted silently;
    * schemas ``partman`` / ``archive`` and the extension;
    * if a *plain* ``public.<t>`` exists (left by :func:`downgrade`), it is renamed to
      ``<t>_legacy`` and its PK/indexes are dropped;
    * ``partman.create_partition`` (``create_parent`` is deprecated since 5.4). For a
      non-empty ``<t>_legacy`` ``p_start_partition`` is the month of ``min(created_at)``
      (capped by now), so every old row gets a real child partition (many partitions for
      very old data are fine - retention handles them on the next ``run_maintenance``);
    * legacy rows are copied with an explicit column list read from
      ``information_schema.columns`` of the new parent (independent of column order), then
      ``partman.partition_data_time`` is looped (a function, runs inside the migration
      transaction) to move whatever landed in ``<t>_default`` (rows newer than the premake
      window) into proper children. ``<t>_default`` is empty after the conversion. Its
      session temp table ``partman_temp_data_storage`` survives until COMMIT and is dropped
      after each parent (otherwise the second parent fails on a column mismatch);
    * ``part_config``: retention literals = ``SystemSettings`` defaults (the real values are
      applied by ``sync_partman_retention.py``); ``infinite_time_partitions = true`` for
      BOTH tables - verified on 5.5.0: with ``false`` ``run_maintenance`` premakes from the
      newest *non-empty* child, so after 3+ months without rows no future partition is
      created, inserts go to ``_default`` and later block the creation of that month;
    * guard trigger (after the UPDATEs), see :data:`GUARD_FUNCTION`.

Downgrade (one migration transaction, no ``undo_partition_proc`` - it COMMITs)
    * guard trigger/function dropped;
    * ``<t>_undo (LIKE <t> INCLUDING ALL EXCLUDING INDEXES)`` - verified on 5.5.0 / PG 17:
      ``INCLUDING ALL`` DOES copy the parent PK and indexes but under generated names, so
      indexes are excluded and recreated with the ``NAMING_CONVENTION`` names;
    * ``undo_partition(p_loop_count := 1, p_keep_table := false)`` in a loop while the
      parent is still in ``part_config`` (5.5.0 deletes the config row and the template
      table itself after the last child), with an iteration limit; template tables listed
      in ``part_config.template_table`` are read beforehand and dropped explicitly as well;
    * ``DROP`` parent -> ``RENAME`` ``<t>_undo`` -> PK + indexes (data preserved, the next
      upgrade converts the plain tables back);
    * tables detached to ``archive`` by retention are no children of the parent any more;
      the whole ``archive`` schema is dropped with ``CASCADE`` (decision R-9) - a detached
      partition never outlives the parent that produced it, and ``upgrade`` on a non-empty
      ``archive`` refuses to run, so nothing can be silently adopted later;
    * ``DROP EXTENSION pg_partman`` then ``DROP SCHEMA partman`` WITHOUT ``CASCADE``: a
      foreign object in ``partman`` makes the downgrade fail loudly instead of being lost.

Known limitation: the trigger guards UPDATE only; DELETE/TRUNCATE of ``part_config`` rows
is not blocked (pg_partman itself deletes the row in ``undo_partition``).

Revision ID: partition_logs
Revises: system_settings
Create Date: 2026-11-06 12:10:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "partition_logs"
down_revision: str | None = "system_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PARTITION_BY = "RANGE (created_at)"
#: Matched by ``app.modules.logging.retention`` together with SQLSTATE ``check_violation``.
GUARD_PREFIX = "PARTMAN_RETENTION_GUARD"
MAX_DATA_MOVE_ITERATIONS = 1000

#: table -> ((index name, columns), ...); names follow NAMING_CONVENTION.
INDEXES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "user_activity": (
        ("ix_user_activity_tenant_id_created_at", ("tenant_id", "created_at")),
        ("ix_user_activity_telegram_user_id_created_at", ("telegram_user_id", "created_at")),
        ("ix_user_activity_bot_id_created_at", ("bot_id", "created_at")),
    ),
    "audit_logs": (
        ("ix_audit_logs_created_at", ("created_at",)),
        ("ix_audit_logs_tenant_id_created_at", ("tenant_id", "created_at")),
    ),
}

#: Guard: for ``public.audit_logs`` (as OLD or NEW row) forbid renaming ``parent_table``,
#: ``retention_keep_table`` other than TRUE, NULL ``retention_schema`` and NULL
#: ``retention``. pg_partman's own UPDATEs (``maintenance_last_run``,
#: ``undo_in_progress``) do not touch these columns (checked in 5.5.0 sources).
GUARD_FUNCTION = f"""
CREATE OR REPLACE FUNCTION public.tg_guard_audit_part_config()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF OLD.parent_table = 'public.audit_logs'
       AND NEW.parent_table IS DISTINCT FROM OLD.parent_table THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = '{GUARD_PREFIX}: parent_table of public.audit_logs cannot be changed';
    END IF;
    IF (OLD.parent_table = 'public.audit_logs' OR NEW.parent_table = 'public.audit_logs')
       AND (NEW.retention_keep_table IS NOT TRUE
            OR NEW.retention_schema IS NULL
            OR NEW.retention IS NULL) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = '{GUARD_PREFIX}: public.audit_logs requires retention_keep_table = true, '
                      'retention_schema IS NOT NULL and retention IS NOT NULL';
    END IF;
    RETURN NEW;
END
$$
"""


def _create_tables() -> None:
    uuid = sa.Uuid()
    op.create_table(
        "user_activity",
        sa.Column("id", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", uuid, nullable=True),
        sa.Column("bot_id", uuid, nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("user_type", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", uuid, nullable=True),
        sa.Column("order_id", uuid, nullable=True),
        sa.Column("product_id", uuid, nullable=True),
        sa.Column("category_id", uuid, nullable=True),
        sa.Column("subcategory_id", uuid, nullable=True),
        sa.Column("case_id", uuid, nullable=True),
        sa.Column("preorder_id", uuid, nullable=True),
        sa.Column("button", sa.Text(), nullable=True),
        sa.Column("callback_data", sa.Text(), nullable=True),
        sa.Column("screen_from", sa.Text(), nullable=True),
        sa.Column("screen_current", sa.Text(), nullable=True),
        sa.Column("screen_to", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", "created_at", name="pk_user_activity"),
        postgresql_partition_by=PARTITION_BY,
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", uuid, nullable=True),
        sa.Column("bot_id", uuid, nullable=True),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", uuid, nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "actor_type IN ('user', 'system', 'bot')", name="ck_audit_logs_actor_type"
        ),
        sa.PrimaryKeyConstraint("id", "created_at", name="pk_audit_logs"),
        postgresql_partition_by=PARTITION_BY,
    )
    for table, indexes in INDEXES.items():
        for name, columns in indexes:
            op.create_index(name, table, list(columns), unique=False)


def _stash_legacy(table: str) -> None:
    drops = " ".join(f"DROP INDEX IF EXISTS public.{name};" for name, _ in INDEXES[table])
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relname = '{table}' AND c.relkind = 'r'
            ) THEN
                ALTER TABLE public.{table} RENAME TO {table}_legacy;
                ALTER TABLE public.{table}_legacy DROP CONSTRAINT IF EXISTS pk_{table};
                {drops}
            END IF;
        END $$;
        """
    )


def _partition(table: str) -> None:
    """``create_partition`` + conversion of ``<t>_legacy`` (if any) without rows in default."""
    op.execute(
        f"""
        DO $$
        DECLARE
            v_has_legacy boolean := to_regclass('public.{table}_legacy') IS NOT NULL;
            v_start text;
            v_columns text;
            v_moved bigint;
            v_iteration integer := 0;
        BEGIN
            IF v_has_legacy THEN
                SELECT date_trunc('month', least(min(created_at), now()))::text
                  INTO v_start
                  FROM public.{table}_legacy;
            END IF;
            PERFORM partman.create_partition(
                p_parent_table    := 'public.{table}',
                p_control         := 'created_at',
                p_interval        := '1 month',
                p_premake         := 3,
                p_default_table   := true,
                p_start_partition := v_start
            );
            IF NOT v_has_legacy THEN
                RETURN;
            END IF;
            SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
              INTO v_columns
              FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = '{table}';
            EXECUTE format(
                'INSERT INTO public.{table} (%1$s) SELECT %1$s FROM public.{table}_legacy',
                v_columns
            );
            DROP TABLE public.{table}_legacy;
            LOOP
                v_moved := partman.partition_data_time(
                    p_parent_table := 'public.{table}',
                    p_batch_count  := {MAX_DATA_MOVE_ITERATIONS}
                );
                EXIT WHEN coalesce(v_moved, 0) = 0;
                v_iteration := v_iteration + 1;
                IF v_iteration > {MAX_DATA_MOVE_ITERATIONS} THEN
                    RAISE EXCEPTION USING MESSAGE =
                        'partition_data_time(public.{table}): default is not drained';
                END IF;
            END LOOP;
            -- partition_data_time keeps its temp table (LIKE the parent) for the whole
            -- transaction; the next parent has other columns, so drop it explicitly.
            DROP TABLE IF EXISTS pg_temp.partman_temp_data_storage;
        END $$;
        """
    )


def upgrade() -> None:
    """Create partitioned log tables, pg_partman config and the guard trigger."""
    op.execute("CREATE SCHEMA IF NOT EXISTS partman")
    # An ``archive`` left behind by an older downgrade keeps detached partitions of a
    # dropped parent (until revision ``partition_logs`` learned to drop it). Upgrading on
    # top of it would silently adopt them as children-less leftovers of the new parent,
    # so refuse loudly: the operator decides whether to import or to discard the rows.
    op.execute(
        """
        DO $$
        DECLARE
            v_left integer;
            v_names text;
        BEGIN
            IF to_regnamespace('archive') IS NULL THEN
                RETURN;
            END IF;
            SELECT count(*), string_agg(relname, ', ' ORDER BY relname)
              INTO v_left, v_names
              FROM pg_class
             WHERE relnamespace = 'archive'::regnamespace
               AND relkind IN ('r', 'p');
            IF v_left > 0 THEN
                RAISE EXCEPTION USING MESSAGE =
                    'schema archive is not empty (' || v_left || ' table(s): '
                    || coalesce(v_names, '?')
                    || '): import or drop them before the upgrade '
                       '(e.g. DROP SCHEMA archive CASCADE)';
            END IF;
        END $$;
        """
    )
    op.execute("CREATE SCHEMA IF NOT EXISTS archive")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman")
    for table in INDEXES:
        _stash_legacy(table)
    _create_tables()
    for table in INDEXES:
        _partition(table)
    op.execute(
        """
        UPDATE partman.part_config
           SET retention                = '24 months',
               retention_schema         = 'archive',
               retention_keep_table     = true,
               retention_keep_index     = true,
               infinite_time_partitions = true
         WHERE parent_table = 'public.audit_logs'
        """
    )
    op.execute(
        """
        UPDATE partman.part_config
           SET retention                = '3 months',
               retention_keep_table     = false,
               infinite_time_partitions = true
         WHERE parent_table = 'public.user_activity'
        """
    )
    op.execute(GUARD_FUNCTION)
    op.execute("REVOKE EXECUTE ON FUNCTION public.tg_guard_audit_part_config() FROM PUBLIC")
    op.execute(
        """
        CREATE TRIGGER guard_audit_part_config
            BEFORE UPDATE ON partman.part_config
            FOR EACH ROW EXECUTE FUNCTION public.tg_guard_audit_part_config()
        """
    )


def _template_guard_sql(table: str) -> str:
    """Invariant check of ``_unpartition``: the partman template of ``table`` is gone.

    ``downgrade()`` drops every ``partman.template_*`` table *before* undoing partitions.
    The check runs **before** the ``undo_partition`` loop - while the ``part_config`` row
    still names the template - and compares exactly (``template_table`` + ``to_regclass``),
    so a template of another parent never trips it. Without a ``part_config`` row for
    ``table`` there is nothing to check and the guard passes.
    """
    return f"""
        DO $$
        BEGIN
            IF to_regclass('partman.part_config') IS NOT NULL AND EXISTS (
                SELECT 1
                  FROM partman.part_config pc
                  JOIN pg_class c ON c.oid = to_regclass(pc.template_table)
                 WHERE pc.parent_table = 'public.{table}'
                   AND c.relkind = 'r'
            ) THEN
                RAISE EXCEPTION USING MESSAGE =
                    'undo_partition(public.{table}): partman template still exists, '
                    'drop templates before undoing the partition';
            END IF;
        END $$;
    """


def _unpartition(table: str) -> None:
    op.execute(_template_guard_sql(table))
    op.execute(
        f"CREATE TABLE public.{table}_undo (LIKE public.{table} INCLUDING ALL EXCLUDING INDEXES)"
    )
    op.execute(
        rf"""
        DO $$
        DECLARE
            iteration integer := 0;
            max_iterations integer := (
                SELECT count(*) FROM pg_inherits WHERE inhparent = 'public.{table}'::regclass
            ) + 5;
        BEGIN
            WHILE EXISTS (
                SELECT 1 FROM partman.part_config WHERE parent_table = 'public.{table}'
            ) LOOP
                iteration := iteration + 1;
                IF iteration > max_iterations THEN
                    RAISE EXCEPTION USING MESSAGE =
                        'undo_partition(public.{table}): no progress after '
                        || max_iterations || ' iterations';
                END IF;
                PERFORM partman.undo_partition(
                    p_parent_table := 'public.{table}',
                    p_target_table := 'public.{table}_undo',
                    p_loop_count   := 1,
                    p_keep_table   := false
                );
            END LOOP;
            IF EXISTS (SELECT 1 FROM pg_inherits WHERE inhparent = 'public.{table}'::regclass)
            THEN
                RAISE EXCEPTION USING MESSAGE =
                    'undo_partition(public.{table}): child partitions remain';
            END IF;
        END $$;
        """
    )
    op.execute(f"DROP TABLE public.{table}")
    op.execute(f"ALTER TABLE public.{table}_undo RENAME TO {table}")
    op.execute(f"ALTER TABLE public.{table} ADD CONSTRAINT pk_{table} PRIMARY KEY (id, created_at)")
    for name, columns in INDEXES[table]:
        op.create_index(name, table, list(columns), unique=False)


def downgrade() -> None:
    """Remove partitioning, keep the rows in plain tables with the same PK/index names.

    The ``archive`` schema of :class:`Retention`-moved partitions is dropped with its rows
    (decision R-9). The drop is announced by a ``NOTICE`` naming the number of archived
    partitions it destroys (decision RC-6) - it is irreversible, and the guard in
    ``upgrade()`` makes a silent re-adoption of stale partitions impossible.
    """
    op.execute("DROP TRIGGER IF EXISTS guard_audit_part_config ON partman.part_config")
    op.execute("DROP FUNCTION IF EXISTS public.tg_guard_audit_part_config()")
    # Template tables also live in ``partman``; read their names before ``undo_partition``
    # deletes the ``part_config`` rows that point at them.
    op.execute(
        """
        DO $$
        DECLARE
            v_template text;
        BEGIN
            FOR v_template IN SELECT template_table FROM partman.part_config LOOP
                IF v_template IS NOT NULL AND to_regclass(v_template) IS NOT NULL THEN
                    EXECUTE format('DROP TABLE %s', to_regclass(v_template));
                END IF;
            END LOOP;
        END $$;
        """
    )
    for table in INDEXES:
        _unpartition(table)
    op.execute("DROP EXTENSION IF EXISTS pg_partman")
    op.execute("DROP SCHEMA IF EXISTS partman")
    # Decision R-9: a table in ``archive`` is a partition *detached from a parent this
    # revision is about to drop*. Keeping it would orphan it (no parent, no
    # ``partman.part_config`` row, nobody owning its lifetime), so the schema goes with its
    # rows. The audit trail itself is intact: the plain ``public.audit_logs`` keeps every
    # row retention had not moved yet, and ``upgrade`` now refuses a non-empty ``archive``
    # instead of adopting stale partitions silently.
    #
    # RC-6: removal is irreversible, so it is announced. Before Alembic 1.16 a template
    # variable was ``:%(name)s``, ``.py`` migrations never print ``op.execute`` results, so
    # the notice is raised SQL-side with the real table count.
    op.execute(
        """
        DO $$
        DECLARE
            v_archived integer := 0;
        BEGIN
            IF to_regnamespace('archive') IS NOT NULL THEN
                SELECT count(*) INTO v_archived
                  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'archive' AND c.relkind IN ('r', 'p');
            END IF;
            IF v_archived > 0 THEN
                RAISE NOTICE
                    'downgrade partition_logs: DROPPING SCHEMA archive CASCADE - '
                    '% archived partition(s) will be deleted irreversibly', v_archived;
            END IF;
        END $$;
        """
    )
    op.execute("DROP SCHEMA IF EXISTS archive CASCADE")
