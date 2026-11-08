"""iam_tenants_languages

IAM reference tables: ``languages`` (seeded ru/en/lv, ``ru`` default), ``tenants`` and
``tenant_settings`` (one row per tenant). Literals are frozen (no app imports).

Revision ID: iam_tenants_languages
Revises: file_logs_retention
Create Date: 2026-11-08 12:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "iam_tenants_languages"
down_revision: str | None = "file_logs_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _business_columns() -> list[sa.Column[object]]:
    """``id`` + timestamps + soft-delete flags of ``BusinessEntity``."""
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.false(), nullable=False),
    ]


def upgrade() -> None:
    """Create languages -> tenants -> tenant_settings and seed the languages."""
    op.create_table(
        "languages",
        *_business_columns(),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.CheckConstraint(
            "code ~ '^[a-z]{2,3}(-[a-z0-9]{2,8})?$'", name=op.f("ck_languages_code_format")
        ),
        sa.CheckConstraint(
            "NOT is_default OR (is_active AND NOT is_deleted)",
            name=op.f("ck_languages_default_is_live"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_languages")),
        sa.UniqueConstraint("code", name=op.f("uq_languages_code")),
    )
    op.create_index(op.f("ix_languages_is_deleted"), "languages", ["is_deleted"], unique=False)
    op.create_index(
        "ix_languages_single_default",
        "languages",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.create_table(
        "tenants",
        *_business_columns(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(slug) <= 64",
            name=op.f("ck_tenants_slug_format"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("slug", name=op.f("uq_tenants_slug")),
    )
    op.create_index(op.f("ix_tenants_is_deleted"), "tenants", ["is_deleted"], unique=False)

    op.create_table(
        "tenant_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("is_store_open", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_buy_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_refill_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_referral_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "is_notifications_enabled", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column("default_language_code", sa.Text(), nullable=True),
        sa.Column("support_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("news_chat_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "faq",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "extra",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["default_language_code"],
            ["languages.code"],
            name=op.f("fk_tenant_settings_default_language_code_languages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_settings_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_settings")),
        sa.UniqueConstraint("tenant_id", name=op.f("uq_tenant_settings_tenant_id")),
    )
    op.create_index(
        op.f("ix_tenant_settings_default_language_code"),
        "tenant_settings",
        ["default_language_code"],
        unique=False,
    )

    op.execute(
        "INSERT INTO languages (id, code, name, is_default) VALUES "
        "(gen_random_uuid(), 'ru', 'Русский', true), "
        "(gen_random_uuid(), 'en', 'English', false), "
        "(gen_random_uuid(), 'lv', 'Latviešu', false)"
    )


def downgrade() -> None:
    """Drop tenant_settings -> tenants -> languages (indexes go with their tables).

    Irreversible data loss: every tenant, its settings and all languages (including ones
    added after the seed) are dropped; a later upgrade restores only the seed languages.
    """
    op.drop_table("tenant_settings")
    op.drop_table("tenants")
    op.drop_table("languages")
