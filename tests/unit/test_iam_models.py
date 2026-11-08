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
    SEED_LANGUAGES,
    TENANT_SLUG_PATTERN,
    Language,
    Tenant,
    TenantSettings,
)
from app.shared_kernel.infrastructure.database import Base

TENANTS = cast(Table, Tenant.__table__)
LANGUAGES = cast(Table, Language.__table__)
SETTINGS = cast(Table, TenantSettings.__table__)


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
