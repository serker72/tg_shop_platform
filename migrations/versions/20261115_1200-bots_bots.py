"""bots

Shop bots (``bots``) and shop customers (``tenant_customers``); log column
``bot_id uuid`` -> ``telegram_bot_id bigint`` in ``user_activity`` / ``audit_logs``.

There is no cast uuid -> bigint (the log stored the internal UUID, the new column stores the
Telegram ID of the bot), so the column is converted ``USING NULL`` and the revision refuses
to run while any bot reference exists: ``user_activity`` must be empty (the column is NOT
NULL), ``audit_logs`` may only hold rows without a bot. RENAME / TYPE on the partitioned
parent propagate to every attached partition (``_default`` included). pg_partman template
tables (``partman.part_config.template_table``) get the same change. Detached partitions in
``archive`` are NOT touched and keep ``bot_id uuid`` (docs/ops/recovery.md).

Revision ID: bots
Revises: iam_rbac
Create Date: 2026-11-15 12:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "bots"
down_revision: str | None = "iam_rbac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PARENTS: tuple[str, ...] = ("user_activity", "audit_logs")
BOT_STATUSES: tuple[str, ...] = ("stopped", "starting", "running", "invalid_token", "error")
OLD_INDEX = "ix_user_activity_bot_id_created_at"
NEW_INDEX = "ix_user_activity_telegram_bot_id_created_at"


def _guard(column: str) -> None:
    """Refuse to convert while a bot reference exists (runbook: docs/ops/recovery.md).

    Both parents are locked first (ACCESS EXCLUSIVE, propagates to the partitions): no row
    can appear between the emptiness check and the ALTERs of this transaction.
    """
    op.execute("LOCK TABLE public.user_activity, public.audit_logs IN ACCESS EXCLUSIVE MODE")
    op.execute(
        f"""
DO $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM public.user_activity) THEN
        RAISE EXCEPTION 'bots: public.user_activity is not empty, column {column} cannot be '
            'converted (no uuid/bigint cast)'
            USING HINT = 'The click-log table must be empty: back it up and run '
                'TRUNCATE public.user_activity (docs/ops/recovery.md, revision bots).';
    END IF;
    IF EXISTS (SELECT 1 FROM public.audit_logs WHERE {column} IS NOT NULL) THEN
        RAISE EXCEPTION 'bots: public.audit_logs has rows with {column} IS NOT NULL, the '
            'column cannot be converted (no uuid/bigint cast)'
            USING HINT = 'Back up audit_logs and run UPDATE public.audit_logs SET {column} = '
                'NULL WHERE {column} IS NOT NULL; never delete audit_logs rows '
                '(docs/ops/recovery.md, revision bots).';
    END IF;
END
$guard$
"""
    )


def _convert(old: str, new: str, type_: str) -> None:
    """RENAME + TYPE on the parents and on the pg_partman template tables (if any)."""
    for table in PARENTS:
        op.execute(f"ALTER TABLE public.{table} RENAME COLUMN {old} TO {new}")
        op.execute(f"ALTER TABLE public.{table} ALTER COLUMN {new} TYPE {type_} USING NULL")
    parents = ", ".join(f"'public.{table}'" for table in PARENTS)
    op.execute(
        f"""
DO $tpl$
DECLARE
    tpl text;
BEGIN
    FOR tpl IN
        SELECT template_table FROM partman.part_config
        WHERE parent_table IN ({parents}) AND template_table IS NOT NULL
    LOOP
        IF EXISTS (
            SELECT 1 FROM pg_attribute
            WHERE attrelid = to_regclass(tpl) AND attname = '{old}' AND NOT attisdropped
        ) THEN
            EXECUTE format('ALTER TABLE %s RENAME COLUMN {old} TO {new}', tpl);
            EXECUTE format('ALTER TABLE %s ALTER COLUMN {new} TYPE {type_} USING NULL', tpl);
        END IF;
    END LOOP;
END
$tpl$
"""
    )


def upgrade() -> None:
    _guard("bot_id")
    _convert("bot_id", "telegram_bot_id", "bigint")
    op.execute(f"ALTER INDEX public.{OLD_INDEX} RENAME TO {NEW_INDEX}")

    op.create_table(
        "bots",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("encrypted_token", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="stopped", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "configuration",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in BOT_STATUSES) + ")",
            name=op.f("ck_bots_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_bots_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bots")),
        sa.UniqueConstraint("telegram_bot_id", name=op.f("uq_bots_telegram_bot_id")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_bots_token_hash")),
    )
    op.create_index(op.f("ix_bots_tenant_id"), "bots", ["tenant_id"])
    op.create_index(op.f("ix_bots_status"), "bots", ["status"])
    op.create_index(op.f("ix_bots_is_deleted"), "bots", ["is_deleted"])

    op.create_table(
        "tenant_customers",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("language_code", sa.Text(), nullable=True),
        sa.Column("first_bot_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_customers_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["language_code"],
            ["languages.code"],
            name=op.f("fk_tenant_customers_language_code_languages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["first_bot_id"],
            ["bots.id"],
            name=op.f("fk_tenant_customers_first_bot_id_bots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_customers")),
        sa.UniqueConstraint(
            "tenant_id",
            "telegram_user_id",
            name=op.f("uq_tenant_customers_tenant_id_telegram_user_id"),
        ),
    )
    op.create_index(
        op.f("ix_tenant_customers_first_bot_id"), "tenant_customers", ["first_bot_id"]
    )
    op.create_index(
        op.f("ix_tenant_customers_language_code"), "tenant_customers", ["language_code"]
    )


def downgrade() -> None:
    _guard("telegram_bot_id")
    op.drop_table("tenant_customers")
    op.drop_table("bots")
    op.execute(f"ALTER INDEX public.{NEW_INDEX} RENAME TO {OLD_INDEX}")
    _convert("telegram_bot_id", "bot_id", "uuid")
