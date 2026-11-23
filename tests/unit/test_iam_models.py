"""Unit tests of the IAM reference models (no database)."""

from __future__ import annotations

import re
from typing import cast

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Table, UniqueConstraint
from sqlalchemy.engine.interfaces import ExecutionContext
from sqlalchemy.sql.schema import CallableColumnDefault

from app.modules.iam.models import (
    DEFAULT_LANGUAGE_CODE,
    LANGUAGE_CODE_PATTERN,
    PERMISSION_SLUG_PATTERN,
    ROLE_SLUG_PATTERN,
    SEED_LANGUAGES,
    TENANT_SLUG_PATTERN,
    Language,
    Permission,
    Role,
    RolePermission,
    Tenant,
    TenantModerator,
    TenantSettings,
    User,
    UserPermissionOverride,
    UserRole,
)
from app.modules.iam.permissions import (
    GLOBAL_PERMISSIONS,
    IAM_RBAC_PERMISSIONS,
    LATER_PERMISSIONS,
    ROLE_ADMIN,
    ROLE_MODERATOR,
    ROLE_SENIOR_MODERATOR,
    SEED_PERMISSION_SLUGS,
    SEED_PERMISSIONS,
    SEED_ROLE_PERMISSIONS,
    SEED_ROLES,
    TENANTS_VIEW_ALL,
)
from app.shared_kernel.infrastructure.database import Base

TENANTS = cast(Table, Tenant.__table__)
LANGUAGES = cast(Table, Language.__table__)
SETTINGS = cast(Table, TenantSettings.__table__)
PERMISSIONS = cast(Table, Permission.__table__)
ROLES = cast(Table, Role.__table__)
ROLE_PERMISSIONS = cast(Table, RolePermission.__table__)
USERS = cast(Table, User.__table__)
USER_ROLES = cast(Table, UserRole.__table__)
OVERRIDES = cast(Table, UserPermissionOverride.__table__)
MODERATORS = cast(Table, TenantModerator.__table__)
RBAC_TABLES = (PERMISSIONS, ROLES, ROLE_PERMISSIONS, USERS, USER_ROLES, OVERRIDES, MODERATORS)


def _fks(table: Table) -> dict[str, tuple[list[str], str, str | None]]:
    return {
        str(fk.name): (list(fk.column_keys), fk.referred_table.name, fk.ondelete)
        for fk in table.constraints
        if isinstance(fk, ForeignKeyConstraint)
    }


def _indexes(table: Table) -> dict[str, tuple[list[str], bool]]:
    return {str(i.name): ([c.name for c in i.columns], bool(i.unique)) for i in table.indexes}


def _checks(table: Table) -> dict[str, str]:
    return {
        str(c.name): str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
    }


def _uniques(table: Table) -> dict[str, list[str]]:
    return {
        str(c.name): [col.name for col in c.columns]
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
    }


def _server_default(table: Table, name: str) -> str:
    default = table.c[name].server_default
    assert default is not None
    return str(getattr(default, "arg", None))


def test_checks() -> None:
    assert _checks(TENANTS) == {
        "ck_tenants_slug_format": "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(slug) <= 64"
    }
    assert _checks(LANGUAGES) == {
        "ck_languages_code_format": "code ~ '^[a-z]{2,3}(-[a-z0-9]{2,8})?$'",
        "ck_languages_default_is_live": "NOT is_default OR (is_active AND NOT is_deleted)",
    }
    assert _checks(SETTINGS) == {}


@pytest.mark.parametrize(
    ("pattern", "value", "ok"),
    [
        (LANGUAGE_CODE_PATTERN, "ru", True),
        (LANGUAGE_CODE_PATTERN, "pt-br", True),
        (LANGUAGE_CODE_PATTERN, "zh-hans", True),
        (LANGUAGE_CODE_PATTERN, "es-419", True),
        (LANGUAGE_CODE_PATTERN, "RU", False),
        (LANGUAGE_CODE_PATTERN, "pt-BR", False),
        (LANGUAGE_CODE_PATTERN, "english", False),
        (LANGUAGE_CODE_PATTERN, "ru-", False),
        (TENANT_SLUG_PATTERN, "my-shop-1", True),
        (TENANT_SLUG_PATTERN, "shop-", False),
        (TENANT_SLUG_PATTERN, "-shop", False),
        (TENANT_SLUG_PATTERN, "a--b", False),
        (TENANT_SLUG_PATTERN, "Shop", False),
    ],
)
def test_patterns(pattern: str, value: str, ok: bool) -> None:
    assert (re.fullmatch(pattern, value) is not None) is ok


def test_uniques() -> None:
    assert _uniques(TENANTS) == {"uq_tenants_slug": ["slug"]}
    assert _uniques(LANGUAGES) == {"uq_languages_code": ["code"]}
    assert _uniques(SETTINGS) == {"uq_tenant_settings_tenant_id": ["tenant_id"]}


def test_languages_single_default_partial_index() -> None:
    indexes = {str(i.name): i for i in LANGUAGES.indexes if isinstance(i, Index)}
    index = indexes["ix_languages_single_default"]
    assert index.unique
    assert [c.name for c in index.columns] == ["is_default"]
    where = index.dialect_options["postgresql"]["where"]
    assert str(where) == "is_default"
    assert set(indexes) == {"ix_languages_single_default", "ix_languages_is_deleted"}


def test_tenant_settings_foreign_keys() -> None:
    fks = {
        str(fk.name): (fk.column_keys, fk.referred_table.name, fk.ondelete, fk.onupdate)
        for fk in SETTINGS.constraints
        if isinstance(fk, ForeignKeyConstraint)
    }
    assert fks == {
        "fk_tenant_settings_tenant_id_tenants": (["tenant_id"], "tenants", "CASCADE", None),
        "fk_tenant_settings_default_language_code_languages": (
            ["default_language_code"],
            "languages",
            "RESTRICT",
            None,
        ),
    }
    index_names = {str(i.name) for i in SETTINGS.indexes}
    assert index_names == {"ix_tenant_settings_default_language_code"}


def test_nullable() -> None:
    nullable = {c.name for c in SETTINGS.columns if c.nullable}
    assert nullable == {"default_language_code", "support_chat_id", "news_chat_id"}
    assert not any(c.nullable for c in TENANTS.columns)
    assert not any(c.nullable for c in LANGUAGES.columns)


def test_tenant_settings_has_no_soft_delete() -> None:
    assert "is_deleted" not in SETTINGS.c
    assert "is_active" not in SETTINGS.c
    assert {"is_deleted", "is_active"} <= set(TENANTS.c.keys())
    assert {"is_deleted", "is_active"} <= set(LANGUAGES.c.keys())


def test_server_defaults_match_python_defaults() -> None:
    expected = {
        "is_store_open": (False, "false"),
        "is_buy_enabled": (True, "true"),
        "is_refill_enabled": (True, "true"),
        "is_referral_enabled": (False, "false"),
        "is_notifications_enabled": (True, "true"),
    }
    for name, (python, server) in expected.items():
        assert getattr(SETTINGS.c[name].default, "arg", None) is python, name
        assert _server_default(SETTINGS, name) == server, name
    for name in ("faq", "extra"):
        assert _server_default(SETTINGS, name) == "'{}'::jsonb"
        default = SETTINGS.c[name].default
        assert isinstance(default, CallableColumnDefault)
        assert default.is_callable
        # SQLAlchemy wraps a zero-argument callable into ``fn(context)``.
        context = cast("ExecutionContext", None)  # the wrapper ignores the context
        first, second = default.arg(context), default.arg(context)
        assert first == {} == second
        assert first is not second
    assert getattr(LANGUAGES.c.is_default.default, "arg", None) is False
    assert _server_default(LANGUAGES, "is_default") == "false"


def test_text_columns_have_no_length() -> None:
    for table, names in (
        (TENANTS, ("name", "slug")),
        (LANGUAGES, ("code", "name")),
        (SETTINGS, ("default_language_code",)),
    ):
        for name in names:
            assert getattr(table.c[name].type, "length", None) is None


def test_eager_defaults() -> None:
    for model in (Tenant, Language, TenantSettings):
        assert model.__mapper__.eager_defaults is True
        assert {"created_at", "updated_at"} <= set(model.__table__.c.keys())


def test_seed_languages() -> None:
    defaults = [code for code, _, is_default in SEED_LANGUAGES if is_default]
    assert defaults == [DEFAULT_LANGUAGE_CODE] == ["ru"]
    assert [code for code, _, _ in SEED_LANGUAGES] == ["ru", "en", "lv"]


def test_registered_in_metadata() -> None:
    assert {"tenants", "languages", "tenant_settings"} <= set(Base.metadata.tables)
    assert {t.name for t in RBAC_TABLES} <= set(Base.metadata.tables)


# --- RBAC --------------------------------------------------------------------------------


def test_rbac_checks() -> None:
    assert _checks(PERMISSIONS) == {
        "ck_permissions_slug_format": "slug ~ '^[a-z][a-z0-9_]*\\.[a-z][a-z0-9_]*$'"
    }
    assert _checks(ROLES) == {
        "ck_roles_slug_format": "slug ~ '^[a-z][a-z0-9_]*$'",
        "ck_roles_system_is_live": "NOT is_system OR (is_active AND NOT is_deleted)",
    }
    assert _checks(USERS) == {"ck_users_telegram_user_id_positive": "telegram_user_id > 0"}
    for table in (ROLE_PERMISSIONS, USER_ROLES, OVERRIDES, MODERATORS):
        assert _checks(table) == {}, table.name


@pytest.mark.parametrize(
    ("pattern", "value", "ok"),
    [
        (PERMISSION_SLUG_PATTERN, "products.view", True),
        (PERMISSION_SLUG_PATTERN, "tenants.view_all", True),
        (PERMISSION_SLUG_PATTERN, "products", False),
        (PERMISSION_SLUG_PATTERN, "products.view.all", False),
        (PERMISSION_SLUG_PATTERN, "Products.view", False),
        (PERMISSION_SLUG_PATTERN, "products_view", False),
        (PERMISSION_SLUG_PATTERN, "1x.view", False),
        (PERMISSION_SLUG_PATTERN, "products.", False),
        (ROLE_SLUG_PATTERN, "senior_moderator", True),
        (ROLE_SLUG_PATTERN, "admin2", True),
        (ROLE_SLUG_PATTERN, "Admin", False),
        (ROLE_SLUG_PATTERN, "senior-moderator", False),
        (ROLE_SLUG_PATTERN, "_admin", False),
        (ROLE_SLUG_PATTERN, "a.b", False),
    ],
)
def test_rbac_patterns(pattern: str, value: str, ok: bool) -> None:
    assert (re.fullmatch(pattern, value) is not None) is ok


def test_rbac_uniques() -> None:
    assert _uniques(PERMISSIONS) == {"uq_permissions_slug": ["slug"]}
    assert _uniques(ROLES) == {"uq_roles_slug": ["slug"]}
    assert _uniques(USERS) == {"uq_users_telegram_user_id": ["telegram_user_id"]}
    assert _uniques(USER_ROLES) == {"uq_user_roles_user_id_role_id": ["user_id", "role_id"]}
    assert _uniques(MODERATORS) == {
        "uq_tenant_moderators_user_id_tenant_id": ["user_id", "tenant_id"]
    }
    name = "uq_user_permission_overrides_user_id_permission_id_tenant_id"
    assert _uniques(OVERRIDES) == {name: ["user_id", "permission_id", "tenant_id"]}
    assert _uniques(ROLE_PERMISSIONS) == {}
    constraint = next(
        c for c in OVERRIDES.constraints if isinstance(c, UniqueConstraint) and c.name == name
    )
    assert constraint.dialect_options["postgresql"]["nulls_not_distinct"] is True


def test_role_permissions_primary_key() -> None:
    assert [c.name for c in ROLE_PERMISSIONS.primary_key.columns] == ["role_id", "permission_id"]
    assert set(ROLE_PERMISSIONS.c.keys()) == {"role_id", "permission_id"}


def test_rbac_foreign_keys() -> None:
    assert _fks(ROLE_PERMISSIONS) == {
        "fk_role_permissions_role_id_roles": (["role_id"], "roles", "CASCADE"),
        "fk_role_permissions_permission_id_permissions": (
            ["permission_id"],
            "permissions",
            "CASCADE",
        ),
    }
    assert _fks(USER_ROLES) == {
        "fk_user_roles_user_id_users": (["user_id"], "users", "CASCADE"),
        "fk_user_roles_role_id_roles": (["role_id"], "roles", "RESTRICT"),
        "fk_user_roles_granted_by_id_users": (["granted_by_id"], "users", "SET NULL"),
    }
    assert _fks(OVERRIDES) == {
        "fk_user_permission_overrides_user_id_users": (["user_id"], "users", "CASCADE"),
        "fk_user_permission_overrides_permission_id_permissions": (
            ["permission_id"],
            "permissions",
            "CASCADE",
        ),
        "fk_user_permission_overrides_tenant_id_tenants": (["tenant_id"], "tenants", "CASCADE"),
        "fk_user_permission_overrides_granted_by_id_users": (
            ["granted_by_id"],
            "users",
            "SET NULL",
        ),
    }
    assert _fks(MODERATORS) == {
        "fk_tenant_moderators_user_id_users": (["user_id"], "users", "CASCADE"),
        "fk_tenant_moderators_tenant_id_tenants": (["tenant_id"], "tenants", "CASCADE"),
    }
    for table in (PERMISSIONS, ROLES, USERS):
        assert _fks(table) == {}, table.name


def test_rbac_indexes() -> None:
    assert _indexes(PERMISSIONS) == {"ix_permissions_is_deleted": (["is_deleted"], False)}
    assert _indexes(ROLES) == {"ix_roles_is_deleted": (["is_deleted"], False)}
    assert _indexes(USERS) == {"ix_users_is_deleted": (["is_deleted"], False)}
    assert _indexes(ROLE_PERMISSIONS) == {
        "ix_role_permissions_permission_id": (["permission_id"], False)
    }
    assert _indexes(USER_ROLES) == {
        "ix_user_roles_role_id": (["role_id"], False),
        "ix_user_roles_granted_by_id": (["granted_by_id"], False),
    }
    assert _indexes(OVERRIDES) == {
        "ix_user_permission_overrides_permission_id": (["permission_id"], False),
        "ix_user_permission_overrides_tenant_id": (["tenant_id"], False),
        "ix_user_permission_overrides_granted_by_id": (["granted_by_id"], False),
    }
    assert _indexes(MODERATORS) == {"ix_tenant_moderators_tenant_id": (["tenant_id"], False)}


def test_every_foreign_key_is_indexed() -> None:
    """In every table of ``Base.metadata`` each FK column leads some index, unique
    constraint or the primary key (no exceptions so far)."""
    from app.core.migrations_support import import_all_models

    import_all_models()
    missing: list[tuple[str, str]] = []
    for table in Base.metadata.tables.values():
        leading = {i.columns[0].name for i in table.indexes if i.columns}
        leading |= {
            next(iter(c.columns)).name
            for c in table.constraints
            if isinstance(c, UniqueConstraint) and c.columns
        }
        if table.primary_key.columns:
            leading.add(next(iter(table.primary_key.columns)).name)
        missing += [
            (table.name, fk.parent.name)
            for fk in table.foreign_keys
            if fk.parent.name not in leading
        ]
    assert missing == []
    assert {t.name for t in RBAC_TABLES} <= set(Base.metadata.tables)


def test_rbac_nullable() -> None:
    def nullable(table: Table) -> set[str]:
        return {c.name for c in table.columns if c.nullable}

    assert nullable(PERMISSIONS) == set()
    assert nullable(ROLES) == set()
    assert nullable(ROLE_PERMISSIONS) == set()
    assert nullable(USERS) == {"telegram_user_name", "timezone"}
    assert nullable(USER_ROLES) == {"granted_by_id"}
    assert nullable(OVERRIDES) == {"tenant_id", "expires_at", "granted_by_id", "reason"}
    assert nullable(MODERATORS) == set()


def test_rbac_soft_delete_split() -> None:
    for table in (PERMISSIONS, ROLES, USERS):
        assert {"is_deleted", "is_active"} <= set(table.c.keys()), table.name
    for table in (ROLE_PERMISSIONS, USER_ROLES, OVERRIDES, MODERATORS):
        assert "is_deleted" not in table.c, table.name
        assert "is_active" not in table.c, table.name


def test_rbac_server_defaults_match_python_defaults() -> None:
    for table, name in ((ROLES, "is_system"), (USERS, "is_superuser")):
        assert getattr(table.c[name].default, "arg", None) is False, name
        assert _server_default(table, name) == "false", name
    assert OVERRIDES.c.is_granted.default is None
    assert OVERRIDES.c.is_granted.server_default is None


def test_rbac_column_types() -> None:
    for table, names in (
        (PERMISSIONS, ("slug", "name")),
        (ROLES, ("slug", "name")),
        (USERS, ("telegram_user_name",)),
        (OVERRIDES, ("reason",)),
    ):
        for name in names:
            assert getattr(table.c[name].type, "length", None) is None
    assert USERS.c.telegram_user_id.type.__class__.__name__ == "BigInteger"
    assert OVERRIDES.c.expires_at.type.timezone is True  # type: ignore[attr-defined]


def test_rbac_eager_defaults() -> None:
    for model in (Permission, Role, User, UserRole, UserPermissionOverride, TenantModerator):
        assert model.__mapper__.eager_defaults is True, model
        assert {"created_at", "updated_at", "id"} <= set(model.__table__.c.keys())
    assert "created_at" not in ROLE_PERMISSIONS.c


def test_seed_permissions() -> None:
    slugs = [slug for slug, _ in SEED_PERMISSIONS]
    # The only place the catalogue size is pinned: 27 TZ 46.7 permissions + tenants.view_all
    # (iam_rbac) + proxies.manage (proxy_pool) + logs.view (staff_permissions).
    assert len(slugs) == len(set(slugs)) == len(SEED_PERMISSION_SLUGS) == 32
    assert GLOBAL_PERMISSIONS <= SEED_PERMISSION_SLUGS
    assert GLOBAL_PERMISSIONS == {
        "roles.manage",
        "settings.manage",
        "tenants.manage",
        "tenants.view_all",
        "moderators.manage",
        "proxies.manage",
        "users.view",
        "users.manage",
        "logs.view",
    }
    assert len(IAM_RBAC_PERMISSIONS) == 28
    assert [s for s, _ in LATER_PERMISSIONS] == [
        "proxies.manage",
        "logs.view",
        "tenants.manage",
        "bots.manage",
    ]
    assert all(re.fullmatch(PERMISSION_SLUG_PATTERN, s) for s in slugs)
    assert all(name.strip() for _, name in SEED_PERMISSIONS)
    assert TENANTS_VIEW_ALL in SEED_PERMISSION_SLUGS


def test_seed_roles() -> None:
    assert [slug for slug, _ in SEED_ROLES] == [ROLE_ADMIN, ROLE_SENIOR_MODERATOR, ROLE_MODERATOR]
    assert all(re.fullmatch(ROLE_SLUG_PATTERN, slug) for slug, _ in SEED_ROLES)
    assert set(SEED_ROLE_PERMISSIONS) == {slug for slug, _ in SEED_ROLES}
    for slugs in SEED_ROLE_PERMISSIONS.values():
        assert slugs <= SEED_PERMISSION_SLUGS
    assert SEED_ROLE_PERMISSIONS[ROLE_ADMIN] == SEED_PERMISSION_SLUGS
    senior = SEED_ROLE_PERMISSIONS[ROLE_SENIOR_MODERATOR]
    assert TENANTS_VIEW_ALL in senior
    assert senior.isdisjoint(
        {
            "roles.manage",
            "settings.manage",
            "accounting.view",
            "accounting.manage",
            "payouts.request",
            "payouts.approve",
            "users.view",
            "users.manage",
            "logs.view",
            "tenants.manage",
            "moderators.manage",
        }
    )
    assert "bots.manage" in senior
    views = {s for s in SEED_PERMISSION_SLUGS if s.endswith(".view")} - {
        "accounting.view",
        "logs.view",
        "users.view",
    }
    assert views <= senior
    assert len(senior) == 20
    moderator = SEED_ROLE_PERMISSIONS[ROLE_MODERATOR]
    assert TENANTS_VIEW_ALL not in moderator
    assert moderator == senior - {TENANTS_VIEW_ALL}
    assert len(moderator) == 19
    assert len(SEED_ROLE_PERMISSIONS[ROLE_ADMIN]) == 32
    assert "proxies.manage" not in senior
