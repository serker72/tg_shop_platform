"""iam_rbac

IAM RBAC tables: ``permissions``, ``roles``, ``role_permissions``, ``users``,
``user_roles``, ``user_permission_overrides``, ``tenant_moderators``. Seeds the permission
catalogue (dot-notation slugs) and the system roles ``admin`` / ``senior_moderator`` /
``moderator`` with their permissions. Literals are frozen (no app imports).

Revision ID: iam_rbac
Revises: iam_tenants_languages
Create Date: 2026-11-08 13:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "iam_rbac"
down_revision: str | None = "iam_tenants_languages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("products.view", "Просмотр товаров"),
    ("products.create", "Создание товаров"),
    ("products.edit", "Редактирование товаров"),
    ("products.delete", "Удаление товаров"),
    ("orders.view", "Просмотр заказов"),
    ("orders.manage", "Управление заказами"),
    ("cases.view", "Просмотр обращений"),
    ("cases.reply", "Ответ на обращения"),
    ("cases.refund", "Возврат по обращению"),
    ("cases.replace", "Замена по обращению"),
    ("cases.close", "Закрытие обращений"),
    ("users.view", "Просмотр пользователей"),
    ("users.manage", "Управление пользователями"),
    ("referrals.view", "Просмотр рефералов"),
    ("reviews.view", "Просмотр отзывов"),
    ("reviews.manage", "Управление отзывами"),
    ("preorders.view", "Просмотр предзаказов"),
    ("preorders.manage", "Управление предзаказами"),
    ("payouts.view", "Просмотр выплат"),
    ("payouts.request", "Запрос выплат"),
    ("payouts.approve", "Одобрение выплат"),
    ("accounting.view", "Просмотр бухгалтерии"),
    ("accounting.manage", "Управление бухгалтерией"),
    ("statistics.view", "Просмотр статистики"),
    ("moderators.manage", "Управление модераторами"),
    ("roles.manage", "Управление ролями"),
    ("settings.manage", "Управление настройками"),
    ("tenants.view_all", "Доступ ко всем магазинам"),
)

ROLES: tuple[tuple[str, str], ...] = (
    ("admin", "Администратор"),
    ("senior_moderator", "Старший модератор"),
    ("moderator", "Модератор"),
)

SENIOR_MODERATOR_PERMISSIONS: tuple[str, ...] = (
    "tenants.view_all",
    "products.view",
    "orders.view",
    "cases.view",
    "users.view",
    "referrals.view",
    "reviews.view",
    "preorders.view",
    "payouts.view",
    "statistics.view",
    "products.create",
    "products.edit",
    "products.delete",
    "orders.manage",
    "cases.reply",
    "cases.refund",
    "cases.replace",
    "cases.close",
    "reviews.manage",
    "preorders.manage",
    "users.manage",
    "moderators.manage",
)

#: ``senior_moderator`` without ``tenants.view_all`` (frozen literals, 21 slugs).
MODERATOR_PERMISSIONS: tuple[str, ...] = (
    "products.view",
    "orders.view",
    "cases.view",
    "users.view",
    "referrals.view",
    "reviews.view",
    "preorders.view",
    "payouts.view",
    "statistics.view",
    "products.create",
    "products.edit",
    "products.delete",
    "orders.manage",
    "cases.reply",
    "cases.refund",
    "cases.replace",
    "cases.close",
    "reviews.manage",
    "preorders.manage",
    "users.manage",
    "moderators.manage",
)


def _timestamp_columns() -> list[sa.Column[object]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def _base_columns() -> list[sa.Column[object]]:
    """``id`` + timestamps of ``BaseEntity``."""
    return [sa.Column("id", sa.Uuid(), nullable=False), *_timestamp_columns()]


def _business_columns() -> list[sa.Column[object]]:
    """``BaseEntity`` columns + soft-delete flags of ``BusinessEntity``."""
    return [
        *_base_columns(),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.false(), nullable=False),
    ]


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _values(rows: Sequence[tuple[str, ...]]) -> str:
    return ", ".join("(" + ", ".join(_literal(v) for v in row) + ")" for row in rows)


def upgrade() -> None:
    """Create the RBAC tables and seed permissions, system roles and their links."""
    op.create_table(
        "permissions",
        *_business_columns(),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "slug ~ '^[a-z][a-z0-9_]*\\.[a-z][a-z0-9_]*$'",
            name=op.f("ck_permissions_slug_format"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
        sa.UniqueConstraint("slug", name=op.f("uq_permissions_slug")),
    )
    op.create_index(op.f("ix_permissions_is_deleted"), "permissions", ["is_deleted"], unique=False)

    op.create_table(
        "roles",
        *_business_columns(),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.CheckConstraint("slug ~ '^[a-z][a-z0-9_]*$'", name=op.f("ck_roles_slug_format")),
        sa.CheckConstraint(
            "NOT is_system OR (is_active AND NOT is_deleted)",
            name=op.f("ck_roles_system_is_live"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("slug", name=op.f("uq_roles_slug")),
    )
    op.create_index(op.f("ix_roles_is_deleted"), "roles", ["is_deleted"], unique=False)

    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("permission_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_role_permissions_permission_id_permissions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_role_permissions_role_id_roles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name=op.f("pk_role_permissions")),
    )
    op.create_index(
        op.f("ix_role_permissions_permission_id"),
        "role_permissions",
        ["permission_id"],
        unique=False,
    )

    op.create_table(
        "users",
        *_business_columns(),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_name", sa.Text(), nullable=True),
        sa.Column("is_superuser", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.CheckConstraint("telegram_user_id > 0", name=op.f("ck_users_telegram_user_id_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("telegram_user_id", name=op.f("uq_users_telegram_user_id")),
    )
    op.create_index(op.f("ix_users_is_deleted"), "users", ["is_deleted"], unique=False)

    op.create_table(
        "user_roles",
        *_base_columns(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("granted_by_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["granted_by_id"],
            ["users.id"],
            name=op.f("fk_user_roles_granted_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_user_roles_role_id_roles"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_roles_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_roles")),
        sa.UniqueConstraint("user_id", "role_id", name=op.f("uq_user_roles_user_id_role_id")),
    )
    op.create_index(
        op.f("ix_user_roles_granted_by_id"), "user_roles", ["granted_by_id"], unique=False
    )
    op.create_index(op.f("ix_user_roles_role_id"), "user_roles", ["role_id"], unique=False)

    op.create_table(
        "user_permission_overrides",
        *_base_columns(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("permission_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("is_granted", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("granted_by_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["granted_by_id"],
            ["users.id"],
            name=op.f("fk_user_permission_overrides_granted_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_user_permission_overrides_permission_id_permissions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_user_permission_overrides_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_permission_overrides_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_permission_overrides")),
        sa.UniqueConstraint(
            "user_id",
            "permission_id",
            "tenant_id",
            name=op.f("uq_user_permission_overrides_user_id_permission_id_tenant_id"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        op.f("ix_user_permission_overrides_granted_by_id"),
        "user_permission_overrides",
        ["granted_by_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_permission_overrides_permission_id"),
        "user_permission_overrides",
        ["permission_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_permission_overrides_tenant_id"),
        "user_permission_overrides",
        ["tenant_id"],
        unique=False,
    )

    op.create_table(
        "tenant_moderators",
        *_base_columns(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_moderators_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_tenant_moderators_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_moderators")),
        sa.UniqueConstraint(
            "user_id", "tenant_id", name=op.f("uq_tenant_moderators_user_id_tenant_id")
        ),
    )
    op.create_index(
        op.f("ix_tenant_moderators_tenant_id"), "tenant_moderators", ["tenant_id"], unique=False
    )

    permission_rows = ", ".join(
        f"(gen_random_uuid(), {_literal(slug)}, {_literal(name)})" for slug, name in PERMISSIONS
    )
    op.execute(f"INSERT INTO permissions (id, slug, name) VALUES {permission_rows}")  # noqa: S608
    role_rows = ", ".join(
        f"(gen_random_uuid(), {_literal(slug)}, {_literal(name)}, true)" for slug, name in ROLES
    )
    op.execute(f"INSERT INTO roles (id, slug, name, is_system) VALUES {role_rows}")  # noqa: S608
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p WHERE r.slug = 'admin'"
    )
    for role, slugs in (
        ("senior_moderator", SENIOR_MODERATOR_PERMISSIONS),
        ("moderator", MODERATOR_PERMISSIONS),
    ):
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "  # noqa: S608
            "SELECT r.id, p.id FROM roles r "
            f"JOIN (VALUES {_values([(s,) for s in slugs])}) AS v(slug) ON true "
            "JOIN permissions p ON p.slug = v.slug "
            f"WHERE r.slug = {_literal(role)}"
        )
    # The JOINs above silently skip an unknown slug: fail loudly instead.
    expected = len(PERMISSIONS) + len(SENIOR_MODERATOR_PERMISSIONS) + len(MODERATOR_PERMISSIONS)
    op.execute(
        "DO $$ DECLARE n bigint; BEGIN "
        "SELECT count(*) INTO n FROM role_permissions; "
        f"IF n <> {expected} THEN "
        f"RAISE EXCEPTION 'iam_rbac: % role_permissions seeded, expected {expected}', n; "
        "END IF; END $$"
    )


def downgrade() -> None:
    """Drop the RBAC tables in reverse dependency order (indexes go with their tables).

    Irreversible data loss: every user, role assignment, permission override, moderator
    assignment and every role/permission (including ones added after the seed) is
    dropped; a later upgrade restores only the seed permissions and system roles.
    """
    op.drop_table("tenant_moderators")
    op.drop_table("user_permission_overrides")
    op.drop_table("user_roles")
    op.drop_table("users")
    op.drop_table("role_permissions")
    op.drop_table("roles")
    op.drop_table("permissions")
