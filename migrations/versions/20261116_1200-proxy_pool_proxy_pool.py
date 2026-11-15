"""proxy_pool

Proxy pool of the bots: ``proxies``, ``bots.proxy_id`` (FK ``ON DELETE SET NULL`` + index)
and the permission ``proxies.manage`` granted to ``admin`` (a new permission is granted to
``admin`` explicitly; the guard below checks that ``admin`` holds every permission).

Revision ID: proxy_pool
Revises: bots
Create Date: 2026-11-16 12:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "proxy_pool"
down_revision: str | None = "bots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROXY_STATUSES: tuple[str, ...] = ("active", "quarantine", "banned")
PERMISSION_SLUG = "proxies.manage"
PERMISSION_NAME = "Управление прокси"


def upgrade() -> None:
    op.create_table(
        "proxies",
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("strike_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_strike_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ban_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
            "status IN (" + ", ".join(f"'{value}'" for value in PROXY_STATUSES) + ")",
            name=op.f("ck_proxies_status"),
        ),
        sa.CheckConstraint(
            "strike_count >= 0", name=op.f("ck_proxies_strike_count_non_negative")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proxies")),
        sa.UniqueConstraint("url_hash", name=op.f("uq_proxies_url_hash")),
    )
    op.create_index(op.f("ix_proxies_status"), "proxies", ["status"])
    op.create_index(op.f("ix_proxies_is_deleted"), "proxies", ["is_deleted"])

    op.add_column("bots", sa.Column("proxy_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_bots_proxy_id_proxies"),
        "bots",
        "proxies",
        ["proxy_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_bots_proxy_id"), "bots", ["proxy_id"])

    op.execute(
        "INSERT INTO permissions (id, slug, name) "
        f"VALUES (gen_random_uuid(), '{PERMISSION_SLUG}', '{PERMISSION_NAME}')"
    )
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r JOIN permissions p "
        f"ON p.slug = '{PERMISSION_SLUG}' WHERE r.slug = 'admin'"
    )
    # admin holds every permission (permissions.py): fail loudly otherwise.
    op.execute(
        "DO $$ DECLARE n bigint; BEGIN "
        "SELECT count(*) INTO n FROM permissions p WHERE NOT EXISTS ("
        "SELECT 1 FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
        "WHERE r.slug = 'admin' AND rp.permission_id = p.id); "
        "IF n <> 0 OR NOT EXISTS (SELECT 1 FROM roles WHERE slug = 'admin') THEN "
        "RAISE EXCEPTION 'proxy_pool: admin misses % permissions', n; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE slug = '{PERMISSION_SLUG}')"
    )
    op.execute(f"DELETE FROM permissions WHERE slug = '{PERMISSION_SLUG}'")
    op.drop_index(op.f("ix_bots_proxy_id"), table_name="bots")
    op.drop_constraint(op.f("fk_bots_proxy_id_proxies"), "bots", type_="foreignkey")
    op.drop_column("bots", "proxy_id")
    op.drop_table("proxies")
