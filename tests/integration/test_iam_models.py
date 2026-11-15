"""IAM reference tables against the migrated test database (savepoint session)."""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import MetaData, Table, create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bot_manager.models import Bot, TenantCustomer
from app.modules.iam.models import (
    SEED_LANGUAGES,
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
    ROLE_ADMIN,
    ROLE_MODERATOR,
    ROLE_SENIOR_MODERATOR,
    SEED_PERMISSION_SLUGS,
    SEED_PERMISSIONS,
    SEED_ROLE_PERMISSIONS,
    SEED_ROLES,
    TENANTS_VIEW_ALL,
)
from app.shared_kernel.infrastructure.base_repository import BaseRepository
from tests.integration.conftest import Query, reseed_languages_sql, reseed_rbac_sql
from tests.integration.conftest import run_alembic as _alembic

pytestmark = pytest.mark.integration

MODEL_CHECK_SCHEMA = "model_check"
IAM_TABLES = (
    "languages",
    "tenants",
    "tenant_settings",
    "permissions",
    "roles",
    "role_permissions",
    "users",
    "user_roles",
    "user_permission_overrides",
    "tenant_moderators",
    "bots",
    "tenant_customers",
)
IAM_MODELS = (
    Language,
    Tenant,
    TenantSettings,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
    UserPermissionOverride,
    TenantModerator,
    Bot,
    TenantCustomer,
)
OVERRIDES_UNIQUE = "uq_user_permission_overrides_user_id_permission_id_tenant_id"

PERMISSIONS_SEED_SQL = "SELECT slug, name FROM permissions ORDER BY slug"
ROLES_SEED_SQL = "SELECT slug, name, is_system FROM roles ORDER BY slug"
ROLE_PERMISSIONS_SEED_SQL = (
    "SELECT r.slug, p.slug FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
    "JOIN permissions p ON p.id = rp.permission_id ORDER BY 1, 2"
)


def _expected_role_permissions() -> list[tuple[str, str]]:
    return sorted((role, perm) for role, perms in SEED_ROLE_PERMISSIONS.items() for perm in perms)


SETTINGS_DEFAULT_COLUMNS = (
    "is_store_open",
    "is_buy_enabled",
    "is_refill_enabled",
    "is_referral_enabled",
    "is_notifications_enabled",
    "faq",
    "extra",
)


async def _tenant(session: AsyncSession, slug: str | None = None) -> Tenant:
    tenant = Tenant(name="Shop", slug=f"shop-{uuid.uuid4().hex[:8]}" if slug is None else slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _language(session: AsyncSession, code: str, *, is_default: bool = False) -> Language:
    language = Language(code=code, name=code, is_default=is_default)
    session.add(language)
    await session.flush()
    return language


async def _user(session: AsyncSession, telegram_user_id: int | None = None) -> User:
    user = User(
        telegram_user_id=uuid.uuid4().int % 10**12 + 1
        if telegram_user_id is None
        else telegram_user_id
    )
    session.add(user)
    await session.flush()
    return user


async def _role(session: AsyncSession, slug: str) -> Role:
    return (await session.execute(select(Role).where(Role.slug == slug))).scalar_one()


async def _permission(session: AsyncSession, slug: str) -> Permission:
    return (await session.execute(select(Permission).where(Permission.slug == slug))).scalar_one()


async def _count(session: AsyncSession, sql: str, **params: object) -> int:
    return int((await session.execute(text(sql), params)).scalar_one())


# --- languages ---------------------------------------------------------------------------


async def test_languages_seeded(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            select(Language.code, Language.is_default)
            .where(Language.is_deleted.is_(False))
            .order_by(Language.code)
        )
    ).all()
    assert {tuple(r) for r in rows} >= {("ru", True), ("en", False), ("lv", False)}
    defaults = [r.code for r in rows if r.is_default]
    assert defaults == ["ru"]


async def test_languages_single_default_rejects_second_default(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ix_languages_single_default"):
        async with session.begin_nested():
            await _language(session, "de", is_default=True)


async def test_languages_single_default_cannot_soft_delete_default(
    session: AsyncSession,
) -> None:
    with pytest.raises(IntegrityError, match="ck_languages_default_is_live"):
        async with session.begin_nested():
            await session.execute(text("UPDATE languages SET is_deleted = true WHERE is_default"))
    # BaseRepository.soft_delete surfaces the same CHECK as IntegrityError on flush.
    ru = (await session.execute(select(Language).where(Language.code == "ru"))).scalar_one()
    with pytest.raises(IntegrityError, match="ck_languages_default_is_live"):
        async with session.begin_nested():
            await BaseRepository(session, Language).soft_delete(ru)


async def test_languages_single_default_cannot_deactivate_default(
    session: AsyncSession,
) -> None:
    with pytest.raises(IntegrityError, match="ck_languages_default_is_live"):
        async with session.begin_nested():
            await session.execute(text("UPDATE languages SET is_active = false WHERE is_default"))
    with pytest.raises(IntegrityError, match="ck_languages_default_is_live"):
        async with session.begin_nested():
            session.add(Language(code="de", name="de", is_default=True, is_active=False))
            await session.flush()


async def test_languages_single_default_switch(session: AsyncSession) -> None:
    """Move the default: clear the old flag first, then set the new one; then the old
    language may be soft-deleted."""
    await session.execute(text("UPDATE languages SET is_default = false WHERE code = 'ru'"))
    await session.execute(text("UPDATE languages SET is_default = true WHERE code = 'en'"))
    await session.execute(text("UPDATE languages SET is_deleted = true WHERE code = 'ru'"))
    rows = (
        await session.execute(
            text(
                "SELECT code, is_deleted FROM languages WHERE code IN ('ru', 'en') AND "
                "(is_default OR is_deleted) ORDER BY code"
            )
        )
    ).all()
    assert [tuple(r) for r in rows] == [("en", False), ("ru", True)]


async def test_languages_single_default_switch_orm(session: AsyncSession) -> None:
    """ORM switch in two flushes (see ``Language`` docstring): the order of UPDATEs
    inside one flush is by primary key, so it is never relied upon."""
    by_code = {
        language.code: language
        for language in (
            await session.execute(select(Language).where(Language.code.in_(("ru", "en"))))
        ).scalars()
    }
    ru, en = by_code["ru"], by_code["en"]
    ru.is_default = False
    await session.flush()
    en.is_default = True
    await session.flush()
    defaults = (
        await session.execute(select(Language.code).where(Language.is_default.is_(True)))
    ).scalars()
    assert list(defaults) == ["en"]
    await BaseRepository(session, Language).soft_delete(ru)
    assert (ru.is_deleted, ru.is_active) == (True, False)


@pytest.mark.parametrize("code", ["RU", "pt-BR", "r", "ru_RU", "ru-", "english", ""])
async def test_language_code_format(session: AsyncSession, code: str) -> None:
    with pytest.raises(IntegrityError, match="ck_languages_code_format"):
        async with session.begin_nested():
            await _language(session, code)


async def test_language_code_format_accepts_valid(session: AsyncSession) -> None:
    existing = set((await session.execute(select(Language.code))).scalars())
    for code in ("ru", "pt-br", "zh-hans", "es-419", "lv"):
        if code in existing:  # seeded rows already passed the CHECK on insert
            await session.execute(
                text("UPDATE languages SET code = code WHERE code = :c"), {"c": code}
            )
        else:
            await _language(session, code)


# --- tenants -----------------------------------------------------------------------------


async def test_tenant_slug_unique_and_format(session: AsyncSession) -> None:
    await _tenant(session, "my-shop-1")
    with pytest.raises(IntegrityError, match="uq_tenants_slug"):
        async with session.begin_nested():
            await _tenant(session, "my-shop-1")
    for slug in ("-shop", "shop-", "a--b", "Shop", "shop_1", "", "a" * 65):
        with pytest.raises(IntegrityError, match="ck_tenants_slug_format"):
            async with session.begin_nested():
                await _tenant(session, slug)
    await _tenant(session, "a" * 64)


# --- tenant_settings ---------------------------------------------------------------------


async def test_tenant_settings_one_per_tenant(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    session.add(TenantSettings(tenant_id=tenant.id))
    await session.flush()
    with pytest.raises(IntegrityError, match="uq_tenant_settings_tenant_id"):
        async with session.begin_nested():
            session.add(TenantSettings(tenant_id=tenant.id))
            await session.flush()


async def test_tenant_settings_cascade_on_tenant_delete(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    settings = TenantSettings(tenant_id=tenant.id, default_language_code="en")
    session.add(settings)
    await session.flush()
    settings_id = settings.id
    session.expunge_all()
    await session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant.id})
    left = (
        await session.execute(
            text("SELECT count(*) FROM tenant_settings WHERE id = :id"), {"id": settings_id}
        )
    ).scalar_one()
    assert left == 0


async def test_tenant_settings_language_fk_restrict(session: AsyncSession) -> None:
    await _language(session, "de")
    tenant = await _tenant(session)
    session.add(TenantSettings(tenant_id=tenant.id, default_language_code="de"))
    await session.flush()
    with pytest.raises(IntegrityError, match="fk_tenant_settings_default_language_code_languages"):
        async with session.begin_nested():
            await session.execute(text("DELETE FROM languages WHERE code = 'de'"))
    other = await _tenant(session)
    with pytest.raises(IntegrityError, match="fk_tenant_settings_default_language_code_languages"):
        async with session.begin_nested():
            session.add(TenantSettings(tenant_id=other.id, default_language_code="xx"))
            await session.flush()


async def test_tenant_settings_null_language_and_unknown_tenant(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    settings = TenantSettings(tenant_id=tenant.id)
    session.add(settings)
    await session.flush()
    assert settings.default_language_code is None
    with pytest.raises(IntegrityError, match="fk_tenant_settings_tenant_id_tenants"):
        async with session.begin_nested():
            session.add(TenantSettings(tenant_id=uuid.uuid4()))
            await session.flush()


async def test_tenant_settings_defaults(session: AsyncSession) -> None:
    """Python ``default=`` (ORM insert) == ``server_default`` (raw insert)."""
    columns = ", ".join(SETTINGS_DEFAULT_COLUMNS)
    orm_tenant = await _tenant(session)
    session.add(TenantSettings(tenant_id=orm_tenant.id))
    await session.flush()
    raw_tenant = await _tenant(session)
    await session.execute(
        text("INSERT INTO tenant_settings (id, tenant_id) VALUES (gen_random_uuid(), :t)"),
        {"t": raw_tenant.id},
    )
    query = text(f"SELECT {columns} FROM tenant_settings WHERE tenant_id = :t")  # noqa: S608
    orm_row = (await session.execute(query, {"t": orm_tenant.id})).one()
    raw_row = (await session.execute(query, {"t": raw_tenant.id})).one()
    assert tuple(orm_row) == tuple(raw_row) == (False, True, True, False, True, {}, {})


# --- RBAC --------------------------------------------------------------------------------


async def test_rbac_seed(session: AsyncSession) -> None:
    permissions = (await session.execute(text(PERMISSIONS_SEED_SQL))).all()
    assert [tuple(r) for r in permissions] == sorted(SEED_PERMISSIONS)
    assert len(permissions) == len(SEED_PERMISSION_SLUGS) == len(SEED_PERMISSIONS)
    roles = (await session.execute(text(ROLES_SEED_SQL))).all()
    assert [tuple(r) for r in roles] == sorted((s, n, True) for s, n in SEED_ROLES)
    links = (await session.execute(text(ROLE_PERMISSIONS_SEED_SQL))).all()
    assert [tuple(r) for r in links] == _expected_role_permissions()
    by_role: dict[str, set[str]] = {}
    for role, perm in links:
        by_role.setdefault(role, set()).add(perm)
    assert by_role[ROLE_ADMIN] == set(SEED_PERMISSION_SLUGS)
    assert TENANTS_VIEW_ALL in by_role[ROLE_SENIOR_MODERATOR]
    assert TENANTS_VIEW_ALL not in by_role[ROLE_MODERATOR]
    assert by_role[ROLE_MODERATOR] == by_role[ROLE_SENIOR_MODERATOR] - {TENANTS_VIEW_ALL}
    assert len(links) == sum(len(p) for p in SEED_ROLE_PERMISSIONS.values())


async def test_role_system_cannot_soft_delete(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_roles_system_is_live"):
        async with session.begin_nested():
            await session.execute(text("UPDATE roles SET is_deleted = true WHERE is_system"))
    admin = await _role(session, ROLE_ADMIN)
    with pytest.raises(IntegrityError, match="ck_roles_system_is_live"):
        async with session.begin_nested():
            await BaseRepository(session, Role).soft_delete(admin)
    custom = Role(slug="custom_role", name="Custom")
    session.add(custom)
    await session.flush()
    await BaseRepository(session, Role).soft_delete(custom)
    assert custom.is_deleted is True


async def test_role_system_cannot_deactivate(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_roles_system_is_live"):
        async with session.begin_nested():
            await session.execute(text("UPDATE roles SET is_active = false WHERE slug = 'admin'"))
    admin = await _role(session, ROLE_ADMIN)
    with pytest.raises(IntegrityError, match="ck_roles_system_is_live"):
        async with session.begin_nested():
            admin.is_active = False
            await session.flush()
    await session.refresh(admin)
    assert admin.is_active is True
    with pytest.raises(IntegrityError, match="ck_roles_system_is_live"):
        async with session.begin_nested():
            session.add(Role(slug="sys_off", name="x", is_system=True, is_active=False))
            await session.flush()
    custom = Role(slug="custom_role", name="Custom")
    session.add(custom)
    await session.flush()
    custom.is_active = False
    await session.flush()
    assert custom.is_active is False


async def test_permission_delete_cascades(session: AsyncSession) -> None:
    user = await _user(session)
    tenant = await _tenant(session)
    role = Role(slug="temp_role", name="Temp")
    permission = Permission(slug="extra.temp", name="x")
    session.add_all([role, permission])
    await session.flush()
    session.add_all(
        [
            RolePermission(role_id=role.id, permission_id=permission.id),
            UserPermissionOverride(user_id=user.id, permission_id=permission.id, is_granted=True),
            UserPermissionOverride(
                user_id=user.id, permission_id=permission.id, tenant_id=tenant.id, is_granted=False
            ),
        ]
    )
    await session.flush()
    permission_id = permission.id
    session.expunge_all()
    await session.execute(text("DELETE FROM permissions WHERE id = :id"), {"id": permission_id})
    for table in ("role_permissions", "user_permission_overrides"):
        sql = f"SELECT count(*) FROM {table} WHERE permission_id = :id"  # noqa: S608
        assert await _count(session, sql, id=permission_id) == 0, table


async def test_tenant_delete_cascades_moderators(session: AsyncSession) -> None:
    user = await _user(session)
    tenant, other = await _tenant(session), await _tenant(session)
    session.add_all(
        [
            TenantModerator(user_id=user.id, tenant_id=tenant.id),
            TenantModerator(user_id=user.id, tenant_id=other.id),
        ]
    )
    await session.flush()
    session.expunge_all()
    await session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant.id})
    sql = "SELECT tenant_id FROM tenant_moderators WHERE user_id = :id"
    rows = (await session.execute(text(sql), {"id": user.id})).scalars().all()
    assert rows == [other.id]


async def test_role_permission_primary_key(session: AsyncSession) -> None:
    admin = await _role(session, ROLE_ADMIN)
    permission = await _permission(session, "products.view")
    with pytest.raises(IntegrityError, match="pk_role_permissions"):
        async with session.begin_nested():
            await session.execute(
                text("INSERT INTO role_permissions (role_id, permission_id) VALUES (:r, :p)"),
                {"r": admin.id, "p": permission.id},
            )


async def test_role_with_assignments_restrict(session: AsyncSession) -> None:
    user = await _user(session)
    role = Role(slug="temp_role", name="Temp")
    session.add(role)
    await session.flush()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    await session.flush()
    with pytest.raises(IntegrityError, match="fk_user_roles_role_id_roles"):
        async with session.begin_nested():
            await session.execute(text("DELETE FROM roles WHERE id = :id"), {"id": role.id})
    with pytest.raises(IntegrityError, match="uq_user_roles_user_id_role_id"):
        async with session.begin_nested():
            session.add(UserRole(user_id=user.id, role_id=role.id))
            await session.flush()


async def test_role_delete_cascades_role_permissions(session: AsyncSession) -> None:
    role = Role(slug="temp_role", name="Temp")
    session.add(role)
    await session.flush()
    permission = await _permission(session, "products.view")
    session.add(RolePermission(role_id=role.id, permission_id=permission.id))
    await session.flush()
    session.expunge_all()
    await session.execute(text("DELETE FROM roles WHERE id = :id"), {"id": role.id})
    sql = "SELECT count(*) FROM role_permissions WHERE role_id = :id"
    assert await _count(session, sql, id=role.id) == 0


async def test_user_delete_cascades(session: AsyncSession) -> None:
    victim, granter, other = await _user(session), await _user(session), await _user(session)
    tenant = await _tenant(session)
    moderator = await _role(session, ROLE_MODERATOR)
    permission = await _permission(session, "orders.view")
    session.add_all(
        [
            UserRole(user_id=victim.id, role_id=moderator.id, granted_by_id=granter.id),
            UserPermissionOverride(
                user_id=victim.id, permission_id=permission.id, is_granted=False
            ),
            TenantModerator(user_id=victim.id, tenant_id=tenant.id),
            UserRole(user_id=other.id, role_id=moderator.id, granted_by_id=victim.id),
            UserPermissionOverride(
                user_id=other.id,
                permission_id=permission.id,
                is_granted=True,
                granted_by_id=victim.id,
            ),
        ]
    )
    await session.flush()
    session.expunge_all()
    await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": victim.id})
    for table in ("user_roles", "user_permission_overrides", "tenant_moderators"):
        sql = f"SELECT count(*) FROM {table} WHERE user_id = :id"  # noqa: S608
        assert await _count(session, sql, id=victim.id) == 0, table
    for table in ("user_roles", "user_permission_overrides"):
        sql = f"SELECT granted_by_id FROM {table} WHERE user_id = :id"  # noqa: S608
        rows = (await session.execute(text(sql), {"id": other.id})).scalars().all()
        assert rows == [None], table


async def test_user_telegram_id_unique_and_positive(session: AsyncSession) -> None:
    await _user(session, 123456789)
    with pytest.raises(IntegrityError, match="uq_users_telegram_user_id"):
        async with session.begin_nested():
            await _user(session, 123456789)
    for value in (0, -1):
        with pytest.raises(IntegrityError, match="ck_users_telegram_user_id_positive"):
            async with session.begin_nested():
                await _user(session, value)
    await _user(session, 2**63 - 1)


async def test_override_unique_nulls_not_distinct(session: AsyncSession) -> None:
    user = await _user(session)
    tenant = await _tenant(session)
    permission = await _permission(session, "orders.view")

    def override(tenant_id: uuid.UUID | None, *, granted: bool) -> UserPermissionOverride:
        return UserPermissionOverride(
            user_id=user.id, permission_id=permission.id, tenant_id=tenant_id, is_granted=granted
        )

    session.add(override(None, granted=True))
    await session.flush()
    with pytest.raises(IntegrityError, match=OVERRIDES_UNIQUE):
        async with session.begin_nested():
            session.add(override(None, granted=False))
            await session.flush()
    session.add(override(tenant.id, granted=False))
    await session.flush()
    with pytest.raises(IntegrityError, match=OVERRIDES_UNIQUE):
        async with session.begin_nested():
            session.add(override(tenant.id, granted=True))
            await session.flush()
    with pytest.raises(IntegrityError, match="is_granted"):
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO user_permission_overrides (id, user_id, permission_id) "
                    "VALUES (gen_random_uuid(), :u, :p)"
                ),
                {"u": user.id, "p": permission.id},
            )


async def test_override_tenant_cascade(session: AsyncSession) -> None:
    user = await _user(session)
    tenant = await _tenant(session)
    permission = await _permission(session, "orders.view")
    session.add(
        UserPermissionOverride(
            user_id=user.id, permission_id=permission.id, tenant_id=tenant.id, is_granted=True
        )
    )
    session.add(
        UserPermissionOverride(user_id=user.id, permission_id=permission.id, is_granted=True)
    )
    await session.flush()
    session.expunge_all()
    await session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant.id})
    sql = "SELECT tenant_id FROM user_permission_overrides WHERE user_id = :id"
    rows = (await session.execute(text(sql), {"id": user.id})).scalars().all()
    assert rows == [None]


async def test_tenant_moderator_unique(session: AsyncSession) -> None:
    user = await _user(session)
    first, second = await _tenant(session), await _tenant(session)
    session.add_all(
        [
            TenantModerator(user_id=user.id, tenant_id=first.id),
            TenantModerator(user_id=user.id, tenant_id=second.id),
        ]
    )
    await session.flush()
    with pytest.raises(IntegrityError, match="uq_tenant_moderators_user_id_tenant_id"):
        async with session.begin_nested():
            session.add(TenantModerator(user_id=user.id, tenant_id=first.id))
            await session.flush()


@pytest.mark.parametrize(
    "slug", ["products", "Products.view", "products.view.all", "products_view", "1a.view", ""]
)
async def test_permission_slug_format(session: AsyncSession, slug: str) -> None:
    with pytest.raises(IntegrityError, match="ck_permissions_slug_format"):
        async with session.begin_nested():
            session.add(Permission(slug=slug, name="x"))
            await session.flush()


async def test_permission_slug_unique(session: AsyncSession) -> None:
    session.add(Permission(slug="extra.do_it", name="x"))
    await session.flush()
    with pytest.raises(IntegrityError, match="uq_permissions_slug"):
        async with session.begin_nested():
            session.add(Permission(slug="products.view", name="dup"))
            await session.flush()


@pytest.mark.parametrize("slug", ["Admin", "senior-moderator", "_x", "a.b", "1role", ""])
async def test_role_slug_format(session: AsyncSession, slug: str) -> None:
    with pytest.raises(IntegrityError, match="ck_roles_slug_format"):
        async with session.begin_nested():
            session.add(Role(slug=slug, name="x"))
            await session.flush()


async def test_rbac_defaults(session: AsyncSession) -> None:
    """Python ``default=`` (ORM insert) == ``server_default`` (raw insert)."""
    orm_user = await _user(session)
    raw_user_id = uuid.uuid4().int % 10**12 + 1
    await session.execute(
        text("INSERT INTO users (id, telegram_user_id) VALUES (gen_random_uuid(), :t)"),
        {"t": raw_user_id},
    )
    user_sql = text(
        "SELECT is_superuser, is_active, is_deleted, telegram_user_name FROM users "
        "WHERE telegram_user_id = :t"
    )
    orm_row = (await session.execute(user_sql, {"t": orm_user.telegram_user_id})).one()
    raw_row = (await session.execute(user_sql, {"t": raw_user_id})).one()
    assert tuple(orm_row) == tuple(raw_row) == (False, True, False, None)

    session.add(Role(slug="orm_role", name="x"))
    await session.flush()
    await session.execute(
        text("INSERT INTO roles (id, slug, name) VALUES (gen_random_uuid(), 'raw_role', 'x')")
    )
    role_sql = text("SELECT is_system, is_active, is_deleted FROM roles WHERE slug = :s")
    orm_role = (await session.execute(role_sql, {"s": "orm_role"})).one()
    raw_role = (await session.execute(role_sql, {"s": "raw_role"})).one()
    assert tuple(orm_role) == tuple(raw_role) == (False, True, False)


# --- scratch database: migration ring and model/schema diff -----------------------------


def test_iam_migration_ring(scratch_db: tuple[str, Query]) -> None:
    url, query = scratch_db
    _alembic(url, command.upgrade, "head")
    seed_sql = "SELECT code, name, is_default FROM languages ORDER BY code"
    assert query(seed_sql) == sorted(SEED_LANGUAGES)
    _alembic(url, command.downgrade, "file_logs_retention")
    assert query(
        "SELECT to_regclass('public.languages'), to_regclass('public.tenants'), "
        "to_regclass('public.tenant_settings')"
    ) == [(None, None, None)]
    _alembic(url, command.upgrade, "head")
    assert query(seed_sql) == sorted(SEED_LANGUAGES)


def _assert_rbac_seed(query: Query) -> None:
    assert query(PERMISSIONS_SEED_SQL) == sorted(SEED_PERMISSIONS)
    assert query(ROLES_SEED_SQL) == sorted((s, n, True) for s, n in SEED_ROLES)
    assert query(ROLE_PERMISSIONS_SEED_SQL) == _expected_role_permissions()


def test_reseed_sql_restores_seed(migrated_scratch_db: tuple[str, Query]) -> None:
    """``clean_db`` re-seed helpers: after TRUNCATE they restore exactly the migration
    seed and are idempotent (run twice)."""
    _, query = migrated_scratch_db
    languages_sql = "SELECT code, name, is_default FROM languages ORDER BY code"
    query("TRUNCATE TABLE permissions, roles, role_permissions, languages CASCADE")
    assert query("SELECT count(*) FROM permissions") == [(0,)]
    assert query("SELECT count(*) FROM languages") == [(0,)]
    for _ in range(2):
        query(reseed_languages_sql())
        for statement in reseed_rbac_sql():
            query(statement)
        assert query(languages_sql) == sorted(SEED_LANGUAGES)
        _assert_rbac_seed(query)


def test_rbac_migration_ring(scratch_db: tuple[str, Query]) -> None:
    url, query = scratch_db
    _alembic(url, command.upgrade, "head")
    _assert_rbac_seed(query)
    _alembic(url, command.downgrade, "iam_tenants_languages")
    rbac = (
        "permissions",
        "roles",
        "role_permissions",
        "users",
        "user_roles",
        "user_permission_overrides",
        "tenant_moderators",
    )
    regclasses = ", ".join(f"to_regclass('public.{name}')" for name in rbac)
    assert query(f"SELECT {regclasses}") == [(None,) * len(rbac)]
    assert query("SELECT to_regclass('public.tenants') IS NOT NULL") == [(True,)]
    _alembic(url, command.upgrade, "head")
    _assert_rbac_seed(query)


@pytest.mark.req("R1")
def test_models_match_migrations(migrated_scratch_db: tuple[str, Query]) -> None:
    """Autogenerate against a fresh ``head`` reports no difference for any model.

    Autogenerate compares neither CHECK constraints nor partial-index ``WHERE``
    clauses. So the IAM tables are also created straight from the model metadata in a
    separate schema ``model_check`` and the catalog definitions (``pg_get_constraintdef``
    for every CHECK, ``pg_get_indexdef`` for every index) are compared for exact equality
    with those built by the migration in ``public``: both sides go through the same PG
    normalization, so any change of an operator, ``AND``/``OR`` or a literal is caught.
    """
    from app.core.migrations_support import import_all_models, include_name, include_object
    from app.shared_kernel.infrastructure.database import Base

    import_all_models()
    url, _ = migrated_scratch_db
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_schemas": True,
                    "include_name": include_name,
                    "include_object": include_object,
                    "target_metadata": Base.metadata,
                },
            )
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
    assert diff == []

    _, query = migrated_scratch_db
    model_metadata = MetaData(naming_convention=Base.metadata.naming_convention)
    for model in IAM_MODELS:
        # FKs between the copied tables follow them into ``model_check``.
        cast(Table, model.__table__).to_metadata(model_metadata, schema=MODEL_CHECK_SCHEMA)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {MODEL_CHECK_SCHEMA}"))
            model_metadata.create_all(connection)
    finally:
        engine.dispose()

    def catalog(schema: str) -> tuple[dict[str, str], dict[str, str]]:
        tables = ", ".join(f"'{schema}.{name}'::regclass" for name in IAM_TABLES)
        checks = query(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "  # noqa: S608
            f"WHERE contype = 'c' AND conrelid IN ({tables})"
        )
        indexes = query(
            "SELECT c.relname, pg_get_indexdef(i.indexrelid) FROM pg_index i "  # noqa: S608
            f"JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid IN ({tables})"
        )
        return (
            {str(r[0]): str(r[1]) for r in checks},
            {str(r[0]): str(r[1]).replace(f" ON {schema}.", " ON ") for r in indexes},
        )

    migrated_checks, migrated_indexes = catalog("public")
    model_checks, model_indexes = catalog(MODEL_CHECK_SCHEMA)
    assert set(migrated_checks) == {
        "ck_bots_status",
        "ck_languages_code_format",
        "ck_languages_default_is_live",
        "ck_tenants_slug_format",
        "ck_permissions_slug_format",
        "ck_roles_slug_format",
        "ck_roles_system_is_live",
        "ck_users_telegram_user_id_positive",
    }
    assert migrated_checks == model_checks
    assert "ix_languages_single_default" in migrated_indexes
    assert migrated_indexes["ix_languages_single_default"].endswith("WHERE is_default")
    assert migrated_indexes[OVERRIDES_UNIQUE].endswith(
        "(user_id, permission_id, tenant_id) NULLS NOT DISTINCT"
    )
    assert migrated_indexes == model_indexes

    def constraints(schema: str) -> dict[str, str]:
        tables = ", ".join(f"'{schema}.{name}'::regclass" for name in IAM_TABLES)
        rows = query(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "  # noqa: S608
            f"WHERE contype IN ('u', 'f', 'p') AND conrelid IN ({tables})"
        )
        return {str(r[0]): str(r[1]).replace(f"{schema}.", "") for r in rows}

    migrated_constraints = constraints("public")
    assert migrated_constraints[OVERRIDES_UNIQUE] == (
        "UNIQUE NULLS NOT DISTINCT (user_id, permission_id, tenant_id)"
    )
    assert migrated_constraints == constraints(MODEL_CHECK_SCHEMA)
