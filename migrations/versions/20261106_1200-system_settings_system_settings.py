"""system_settings

Singleton table of platform-wide settings (``id = 1``) and its single row.
Defaults are copied as literals (revisions are frozen; they never import app constants).

Revision ID: system_settings
Revises: initial
Create Date: 2026-11-06 12:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "system_settings"
down_revision: str | None = "initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``system_settings`` and insert the singleton row."""
    op.create_table(
        "system_settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("is_maintenance_mode", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "iam_cache_ttl_seconds", sa.Integer(), server_default=sa.text("86400"), nullable=False
        ),
        sa.Column(
            "extra_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "user_activity_retention_months",
            sa.Integer(),
            server_default=sa.text("3"),
            nullable=False,
        ),
        sa.Column(
            "audit_logs_retention_months",
            sa.Integer(),
            server_default=sa.text("24"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_system_settings_singleton")),
        sa.CheckConstraint(
            "iam_cache_ttl_seconds > 0", name=op.f("ck_system_settings_iam_cache_ttl_positive")
        ),
        sa.CheckConstraint(
            "user_activity_retention_months BETWEEN 1 AND 120",
            name=op.f("ck_system_settings_user_activity_retention_range"),
        ),
        sa.CheckConstraint(
            "audit_logs_retention_months BETWEEN 1 AND 120",
            name=op.f("ck_system_settings_audit_logs_retention_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_system_settings")),
    )
    op.execute("INSERT INTO system_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")


def downgrade() -> None:
    """Drop ``system_settings``."""
    op.drop_table("system_settings")
