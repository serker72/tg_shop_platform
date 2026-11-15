"""Pure business rules of the IAM admin services (no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.core.observability.metrics import build_metrics, render_metrics
from app.modules.iam import admin_service as svc
from app.modules.iam.admin_repository import OVERRIDE_SLOT_CONSTRAINT, build_override_upsert
from app.modules.iam.cache import InvalidationResult
from app.modules.iam.metrics import CacheInvalidationFailures
from app.modules.iam.permissions import GLOBAL_PERMISSIONS, PRODUCTS_VIEW
from app.shared_kernel.domain.exceptions import (
    EntityAlreadyExistsError,
    EntityNotFoundError,
    PermissionDeniedError,
    ValidationError,
)

pytestmark = pytest.mark.req("R-II.2")

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _user(**kw: Any) -> Any:
    return SimpleNamespace(id=kw.get("id", uuid4()), is_superuser=kw.get("is_superuser", False))


def _session(**pending: Any) -> AsyncMock:
    session = AsyncMock()
    session.new, session.dirty, session.deleted = (), (), ()
    for key, value in pending.items():
        setattr(session, key, value)
    return session


def _service(
    session: AsyncMock, permissions: Any = None, failures: CacheInvalidationFailures | None = None
) -> svc.UserAdminService:
    return svc.UserAdminService(
        session,
        permissions or AsyncMock(),
        audit=AsyncMock(),
        repository=AsyncMock(),
        invalidation_failures=failures or CacheInvalidationFailures(),
    )


@pytest.mark.parametrize("slug", sorted(GLOBAL_PERMISSIONS))
def test_global_permission_tenant_override_rejected(slug: str) -> None:
    with pytest.raises(ValidationError) as info:
        svc.validate_override_scope(slug, uuid4())
    assert info.value.code == svc.GLOBAL_PERMISSION_TENANT_OVERRIDE
    svc.validate_override_scope(slug, None)


def test_tenant_override_of_regular_permission_allowed() -> None:
    svc.validate_override_scope(PRODUCTS_VIEW, uuid4())


@pytest.mark.parametrize("expires_at", [NOW, NOW - timedelta(seconds=1)])
def test_expires_at_must_be_future(expires_at: datetime) -> None:
    with pytest.raises(ValidationError) as info:
        svc.validate_expires_at(expires_at, NOW)
    assert info.value.code == svc.EXPIRES_AT_NOT_IN_FUTURE


def test_naive_expires_at_has_own_code() -> None:
    with pytest.raises(ValidationError) as info:
        svc.validate_expires_at(datetime(2030, 1, 1), NOW)  # noqa: DTZ001
    assert info.value.code == svc.INVALID_EXPIRES_AT


def test_expires_at_future_or_none_ok() -> None:
    svc.validate_expires_at(None, NOW)
    svc.validate_expires_at(NOW + timedelta(seconds=1), NOW)


def test_self_modification_forbidden() -> None:
    actor = _user()
    with pytest.raises(PermissionDeniedError) as info:
        svc.ensure_not_self(actor, actor)
    assert info.value.code == svc.SELF_MODIFICATION_FORBIDDEN
    svc.ensure_not_self(actor, _user())


def test_superuser_protected_from_non_superuser() -> None:
    with pytest.raises(PermissionDeniedError) as info:
        svc.ensure_superuser_protected(_user(), _user(is_superuser=True))
    assert info.value.code == svc.SUPERUSER_PROTECTED
    svc.ensure_superuser_protected(_user(is_superuser=True), _user(is_superuser=True))


def test_system_role_frozen() -> None:
    with pytest.raises(ValidationError) as info:
        svc.ensure_system_role_mutable(SimpleNamespace(is_system=True, slug="admin"), "x")  # type: ignore[arg-type]
    assert info.value.code == svc.SYSTEM_ROLE_PROTECTED
    svc.ensure_system_role_mutable(SimpleNamespace(is_system=False, slug="c"), "x")  # type: ignore[arg-type]


@pytest.mark.parametrize("slug", ["", "A", "-a", "a-", "a--b", "a_b", "x" * 65])
def test_bad_tenant_slug(slug: str) -> None:
    with pytest.raises(ValidationError) as info:
        svc.validate_tenant_slug(slug)
    assert info.value.code == svc.INVALID_TENANT_SLUG


@pytest.mark.parametrize("slug", ["", "Admin", "1x", "a-b"])
def test_bad_role_slug(slug: str) -> None:
    with pytest.raises(ValidationError) as info:
        svc.validate_role_slug(slug)
    assert info.value.code == svc.INVALID_ROLE_SLUG


@pytest.mark.parametrize("value", [0, -1, True])
def test_bad_telegram_id(value: int) -> None:
    with pytest.raises(ValidationError) as info:
        svc.validate_telegram_user_id(value)
    assert info.value.code == svc.INVALID_TELEGRAM_USER_ID


def test_names_and_usernames() -> None:
    assert svc.validate_name("  Shop ") == "Shop"
    with pytest.raises(ValidationError) as info:
        svc.validate_name("   ")
    assert info.value.code == svc.INVALID_NAME
    assert svc.normalize_username(" @john ") == "john"
    assert svc.normalize_username("@") is None
    assert svc.normalize_username(None) is None


def test_tenant_settings_validation() -> None:
    assert svc.validate_tenant_settings({"is_store_open": True, "support_chat_id": -100}) == {
        "is_store_open": True,
        "support_chat_id": -100,
    }
    bad_values: list[dict[str, Any]] = [
        {"faq": None},
        {"is_store_open": 1},
        {"news_chat_id": True},
        {"default_language_code": ""},
    ]
    for bad in bad_values:
        with pytest.raises(ValidationError) as info:
            svc.validate_tenant_settings(bad)
        assert info.value.code == svc.INVALID_SETTINGS


def test_permission_slug_format() -> None:
    with pytest.raises(ValidationError) as info:
        svc.validate_permission_slugs(["products.view", "bad"])
    assert info.value.code == svc.PERMISSION_NOT_FOUND


def _integrity(sqlstate: str | None, constraint: str | None) -> IntegrityError:
    orig = SimpleNamespace(sqlstate=sqlstate, diag=SimpleNamespace(constraint_name=constraint))
    return IntegrityError("INSERT secret sql", {"p": "secret"}, orig)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("sqlstate", "constraint", "error_type", "code"),
    [
        ("23505", "uq_tenants_slug", EntityAlreadyExistsError, svc.TENANT_SLUG_TAKEN),
        ("23505", "uq_roles_slug", EntityAlreadyExistsError, svc.ROLE_SLUG_TAKEN),
        ("23505", "uq_users_telegram_user_id", EntityAlreadyExistsError, svc.USER_EXISTS),
        (
            "23505",
            "uq_user_roles_user_id_role_id",
            EntityAlreadyExistsError,
            svc.ROLE_ALREADY_ASSIGNED,
        ),
        (
            "23505",
            "uq_tenant_moderators_user_id_tenant_id",
            EntityAlreadyExistsError,
            svc.MODERATOR_ALREADY_ASSIGNED,
        ),
        ("23505", "uq_unknown", EntityAlreadyExistsError, svc.CONFLICT),
        ("23503", "fk_user_roles_role_id_roles", EntityNotFoundError, svc.ROLE_NOT_FOUND),
        ("23503", "fk_user_roles_user_id_users", EntityNotFoundError, svc.USER_NOT_FOUND),
        ("23503", "fk_x_tenant_id_tenants", EntityNotFoundError, svc.TENANT_NOT_FOUND),
        ("23503", "fk_weird", EntityAlreadyExistsError, svc.CONFLICT),
        ("23514", "ck_roles_system_is_live", EntityAlreadyExistsError, svc.CONFLICT),
        (None, None, EntityAlreadyExistsError, svc.CONFLICT),
    ],
)
def test_map_integrity_error(
    sqlstate: str | None, constraint: str | None, error_type: type, code: str
) -> None:
    error = svc.map_integrity_error(_integrity(sqlstate, constraint))
    assert type(error) is error_type and error.code == code
    assert "secret" not in str(error) and "uq_" not in str(error)


def test_override_upsert_targets_slot_constraint() -> None:
    stmt = build_override_upsert(
        user_id=uuid4(),
        permission_id=uuid4(),
        tenant_id=None,
        is_granted=False,
        expires_at=None,
        reason=None,
        granted_by_id=None,
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
    assert f"ON CONFLICT ON CONSTRAINT {OVERRIDE_SLOT_CONSTRAINT} DO UPDATE" in sql
    for column in ("is_granted", "expires_at", "reason", "granted_by_id", "updated_at"):
        assert f"{column} = " in sql


async def test_foreign_pending_changes_rejected() -> None:
    session = _session(dirty={object()})
    service = _service(session)
    with pytest.raises(ValidationError) as info:
        async with service._transaction():
            pass
    assert info.value.code == svc.SESSION_NOT_CLEAN
    session.commit.assert_not_awaited()


async def test_invalidation_after_commit_without_retry_reports_failure() -> None:
    calls: list[str] = []
    session = _session()
    session.commit.side_effect = lambda: calls.append("commit")
    permissions = AsyncMock()

    async def invalidate(tg: int) -> InvalidationResult:
        calls.append(f"invalidate:{tg}")
        return InvalidationResult(ok=False)

    permissions.invalidate.side_effect = invalidate
    failures = CacheInvalidationFailures()
    service = _service(session, permissions, failures)
    async with service._transaction():
        calls.append("work")
    assert await service._invalidate([7]) is False
    assert calls == ["work", "commit", "invalidate:7"]
    assert service.cache_invalidated is False
    assert failures.by_scope == {"user": 1}

    permissions.invalidate_all.return_value = InvalidationResult(ok=True)
    async with service._transaction():
        pass
    assert service.cache_invalidated is True  # reset per call
    assert await service._invalidate(everything=True) is True


async def test_no_change_commits_read_and_skips_invalidation() -> None:
    session = _session()
    permissions = AsyncMock()
    service = _service(session, permissions)
    async with service._transaction():
        raise svc._NoChange
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    assert await service._invalidate([7]) is True
    permissions.invalidate.assert_not_awaited()


async def test_error_rolls_back_without_commit() -> None:
    session = _session()
    service = _service(session)
    with pytest.raises(ValidationError):
        async with service._transaction():
            raise ValidationError("x")
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


async def test_integrity_error_mapped_in_transaction() -> None:
    session = _session()
    service = _service(session)
    with pytest.raises(EntityAlreadyExistsError) as info:
        async with service._transaction():
            raise _integrity("23505", "uq_roles_slug")
    assert info.value.code == svc.ROLE_SLUG_TAKEN
    session.rollback.assert_awaited_once()


async def test_update_username_unchanged_is_plain_read() -> None:
    session = _session()
    repo = AsyncMock()
    repo.get_user_by_telegram_id.return_value = SimpleNamespace(telegram_user_name="john")
    service = svc.UserAdminService(session, AsyncMock(), audit=AsyncMock(), repository=repo)
    user = await service.update_username(5, "@john")
    assert user is not None and user.telegram_user_name == "john"
    repo.get_user_by_telegram_id.assert_awaited_once_with(5)
    session.commit.assert_not_awaited()


def test_invalidation_failures_metric_exported() -> None:
    text = render_metrics(build_metrics().registry).decode()
    assert "# TYPE iam_cache_invalidation_failures_total counter" in text
    for scope in ("all", "user"):
        assert f'iam_cache_invalidation_failures_total{{scope="{scope}"}}' in text
