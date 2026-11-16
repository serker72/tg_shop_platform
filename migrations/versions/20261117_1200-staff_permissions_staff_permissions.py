"""staff_permissions

Final staff role model (one revision for every permission change of milestone II):

* new permissions: ``logs.view`` (admin), ``tenants.manage`` (global, admin: shops and their
  settings; ``settings.manage`` keeps system parameters only), ``bots.manage`` (per shop:
  admin, senior_moderator, moderator);
* ``users.manage`` and ``moderators.manage`` are taken from ``senior_moderator`` and
  ``moderator`` (by slug; personal overrides stay);
* tenant-scoped overrides of ``users.view`` / ``users.manage`` (they become global) stop the
  upgrade - resolve them by hand;
* shop assignments (``tenant_moderators``) of superusers and of staff holding a global
  ``tenants.view_all`` (live role or global allow) are deleted, each with an ``audit_logs``
  record (``actor_type='system'``, ``iam.moderator.unassign``, reason ``global_access``);
* ``tenant_customers`` -> ``customers`` with every constraint / index (``NAMING_CONVENTION``).

Downgrade is symmetric except the deleted shop assignments: they are NOT restored (the audit
records keep them).

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

NEW_PERMISSIONS = (
    ("logs.view", "Просмотр логов", "('admin')"),
    ("tenants.manage", "Управление магазинами", "('admin')"),
    ("bots.manage", "Управление ботами", "('admin', 'senior_moderator', 'moderator')"),
)
NEW_SLUGS = "('logs.view', 'tenants.manage', 'bots.manage')"
#: Taken from the moderator roles by the upgrade.
REVOKED_SLUGS = "('users.manage', 'moderators.manage')"
MODERATOR_ROLES = "('senior_moderator', 'moderator')"
#: Expected number of permissions of the system roles after the upgrade.
EXPECTED_LINKS = {"admin": 32, "senior_moderator": 21, "moderator": 20}
#: ... and after the downgrade (state of ``proxy_pool``).
DOWNGRADE_LINKS = {"admin": 29, "senior_moderator": 22, "moderator": 21}

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


_GLOBAL_ASSIGNMENTS = (
    "SELECT tm.id, tm.user_id, tm.tenant_id FROM tenant_moderators tm "
    "JOIN users u ON u.id = tm.user_id WHERE u.is_superuser OR (EXISTS ("
    "SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id "
    "JOIN role_permissions rp ON rp.role_id = r.id JOIN permissions p ON p.id = rp.permission_id "
    "WHERE ur.user_id = u.id AND r.is_active AND NOT r.is_deleted AND p.is_active "
    "AND NOT p.is_deleted AND p.slug = 'tenants.view_all') OR EXISTS ("
    "SELECT 1 FROM user_permission_overrides o JOIN permissions p ON p.id = o.permission_id "
    "WHERE o.user_id = u.id AND o.tenant_id IS NULL AND o.is_granted AND p.is_active "
    "AND NOT p.is_deleted AND p.slug = 'tenants.view_all' "
    "AND (o.expires_at IS NULL OR o.expires_at > now()))) AND NOT EXISTS ("
    "SELECT 1 FROM user_permission_overrides d JOIN permissions p ON p.id = d.permission_id "
    "WHERE d.user_id = u.id AND d.tenant_id IS NULL AND NOT d.is_granted "
    "AND p.slug = 'tenants.view_all' AND (d.expires_at IS NULL OR d.expires_at > now()) "
    "AND NOT u.is_superuser)"
)


def upgrade() -> None:
    # users.view / users.manage become global: a tenant-scoped override of them would be
    # silently ignored by the resolver, so refuse to migrate until it is resolved by hand.
    op.execute(
        "DO $$ DECLARE n bigint; BEGIN "
        "SELECT count(*) INTO n FROM user_permission_overrides o "
        "JOIN permissions p ON p.id = o.permission_id "
        "WHERE o.tenant_id IS NOT NULL AND p.slug IN ('users.view', 'users.manage'); "
        "IF n <> 0 THEN RAISE EXCEPTION 'staff_permissions: % tenant-scoped overrides of "
        "users.view/users.manage exist; these permissions become global - delete them or "
        "replace with global overrides (tenant_id IS NULL) before upgrading', n; "
        "END IF; END $$"
    )
    for slug, name, roles in NEW_PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, slug, name) "
            f"VALUES (gen_random_uuid(), '{slug}', '{name}')"
        )
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r JOIN permissions p "
            f"ON p.slug = '{slug}' WHERE r.slug IN {roles}"
        )
    op.execute(
        "DELETE FROM role_permissions rp USING roles r, permissions p "
        "WHERE r.id = rp.role_id AND p.id = rp.permission_id "
        f"AND p.slug IN {REVOKED_SLUGS} AND r.slug IN {MODERATOR_ROLES}"
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
    # Staff with global access sees every shop: drop its assignments, with audit.
    op.execute(
        f"WITH gone AS (DELETE FROM tenant_moderators WHERE id IN (SELECT id FROM "
        f"({_GLOBAL_ASSIGNMENTS}) g) RETURNING user_id, tenant_id) "
        "INSERT INTO audit_logs (id, created_at, tenant_id, actor_type, action, entity_type, "
        "entity_id, payload, result) SELECT gen_random_uuid(), now(), g.tenant_id, 'system', "
        "'iam.moderator.unassign', 'user', g.user_id, jsonb_build_object('user_id', g.user_id, "
        "'telegram_user_id', u.telegram_user_id, 'tenant_id', g.tenant_id, "
        "'reason', 'global_access', 'revision', 'staff_permissions'), "
        "'success' FROM gone g JOIN users u ON u.id = g.user_id"
    )
    _rename_table(OLD_TABLE, NEW_TABLE)


def downgrade() -> None:
    _rename_table(NEW_TABLE, OLD_TABLE)
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r JOIN permissions p "
        f"ON p.slug IN {REVOKED_SLUGS} WHERE r.slug IN {MODERATOR_ROLES} "
        "ON CONFLICT DO NOTHING"
    )
    for table in ("user_permission_overrides", "role_permissions"):
        op.execute(
            f"DELETE FROM {table} WHERE permission_id IN "
            f"(SELECT id FROM permissions WHERE slug IN {NEW_SLUGS})"
        )
    op.execute(f"DELETE FROM permissions WHERE slug IN {NEW_SLUGS}")
    _guard_links("downgrade", DOWNGRADE_LINKS)
