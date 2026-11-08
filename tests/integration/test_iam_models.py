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

from app.modules.iam.models import (
    SEED_LANGUAGES,
    Language,
    Tenant,
    TenantSettings,
)
from app.shared_kernel.infrastructure.base_repository import BaseRepository
from tests.integration.conftest import Query
from tests.integration.conftest import run_alembic as _alembic

pytestmark = pytest.mark.integration

MODEL_CHECK_SCHEMA = "model_check"
IAM_TABLES = ("languages", "tenants", "tenant_settings")

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
    for model in (Language, Tenant, TenantSettings):
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
        "ck_languages_code_format",
        "ck_languages_default_is_live",
        "ck_tenants_slug_format",
    }
    assert migrated_checks == model_checks
    assert "ix_languages_single_default" in migrated_indexes
    assert migrated_indexes["ix_languages_single_default"].endswith("WHERE is_default")
    assert migrated_indexes == model_indexes
