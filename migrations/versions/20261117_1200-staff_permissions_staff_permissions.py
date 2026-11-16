"""staff_permissions

Staff permissions model: ``users.*`` are about staff only. Adds ``logs.view`` granted to
``admin``; takes ``users.manage`` away from ``senior_moderator`` and ``moderator`` (by slug;
personal overrides are left as they are); renames ``tenant_customers`` -> ``customers``
with every constraint / index following ``NAMING_CONVENTION``.

Revision ID: staff_permissions
Revises: proxy_pool
Create Date: 2026-11-17 12:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "staff_permissions"
down_revision: str | None = "proxy_pool"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOGS_SLUG = "logs.view"
LOGS_NAME = "Просмотр логов"
USERS_MANAGE = "users.manage"
MODERATOR_ROLES = "('senior_moderator', 'moderator')"
#: Expected number of permissions of the system roles after the upgrade.
EXPECTED_LINKS = {"senior_moderator": 21, "moderator": 20}

OLD_TABLE, NEW_TABLE = "tenant_customers", "customers"
#: Constraint suffixes (pk, fk x3, uq) and index suffixes (ix x2) of the table.
CONSTRAINTS = (
    "pk_{t}",
    "fk_{t}_tenant_id_tenants",
    "fk_{t}_language_code_languages",
    "fk_{t}_first_bot_id_bots",
    "uq_{t}_tenant_id_telegram_user_id",
)
INDEXES = ("ix_{t}_first_bot_id", "ix_{t}_language_code")


def _rename_table(old: str, new: str) -> None:
    op.rename_table(old, new)
    for name in CONSTRAINTS:
        op.execute(
            f"ALTER TABLE {new} RENAME CONSTRAINT {name.format(t=old)} TO {name.format(t=new)}"
        )
    for name in INDEXES:
        op.execute(f"ALTER INDEX {name.format(t=old)} RENAME TO {name.format(t=new)}")


def _guard_links(stage: str, expected: dict[str, int]) -> None:
    for role, count in expected.items():
        op.execute(
            "DO $$ DECLARE n bigint; BEGIN "
            "SELECT count(*) INTO n FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
            f"WHERE r.slug = '{role}'; "
            f"IF n <> {count} THEN "
            f"RAISE EXCEPTION 'staff_permissions {stage}: {role} has % permissions, "
            f"expected {count}', n; "
            "END IF; END $$"
        )


def upgrade() -> None:
    op.execute(
        "INSERT INTO permissions (id, slug, name) "
        f"VALUES (gen_random_uuid(), '{LOGS_SLUG}', '{LOGS_NAME}')"
    )
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r JOIN permissions p "
        f"ON p.slug = '{LOGS_SLUG}' WHERE r.slug = 'admin'"
    )
    op.execute(
        "DELETE FROM role_permissions rp USING roles r, permissions p "
        "WHERE r.id = rp.role_id AND p.id = rp.permission_id "
        f"AND p.slug = '{USERS_MANAGE}' AND r.slug IN {MODERATOR_ROLES}"
    )
    # admin holds every permission (permissions.py): fail loudly otherwise.
    op.execute(
        "DO $$ DECLARE n bigint; BEGIN "
        "SELECT count(*) INTO n FROM permissions p WHERE NOT EXISTS ("
        "SELECT 1 FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
        "WHERE r.slug = 'admin' AND rp.permission_id = p.id); "
        "IF n <> 0 OR NOT EXISTS (SELECT 1 FROM roles WHERE slug = 'admin') THEN "
        "RAISE EXCEPTION 'staff_permissions: admin misses % permissions', n; "
        "END IF; END $$"
    )
    _guard_links("upgrade", EXPECTED_LINKS)
    _rename_table(OLD_TABLE, NEW_TABLE)


def downgrade() -> None:
    _rename_table(NEW_TABLE, OLD_TABLE)
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r JOIN permissions p "
        f"ON p.slug = '{USERS_MANAGE}' WHERE r.slug IN {MODERATOR_ROLES} "
        "ON CONFLICT DO NOTHING"
    )
    op.execute(
        "DELETE FROM user_permission_overrides WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE slug = '{LOGS_SLUG}')"
    )
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE slug = '{LOGS_SLUG}')"
    )
    op.execute(f"DELETE FROM permissions WHERE slug = '{LOGS_SLUG}'")
    _guard_links("downgrade", {role: n + 1 for role, n in EXPECTED_LINKS.items()})
