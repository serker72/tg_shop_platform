"""Pure business rules of the IAM admin services (no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.modules.iam import admin_service as svc
from app.modules.iam.cache import InvalidationResult
from app.modules.iam.permissions import GLOBAL_PERMISSIONS, PRODUCTS_VIEW
from app.shared_kernel.domain.exceptions import PermissionDeniedError, ValidationError

pytestmark = pytest.mark.req("R-II.2")

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _user(**kw: Any) -> Any:
    return SimpleNamespace(id=kw.get("id", uuid4()), is_superuser=kw.get("is_superuser", False))


@pytest.mark.parametrize("slug", sorted(GLOBAL_PERMISSIONS))
def test_global_permission_tenant_override_rejected(slug: str) -> None:
    with pytest.raises(ValidationError) as info:
        svc.validate_override_scope(slug, uuid4())
    assert info.value.code == svc.GLOBAL_PERMISSION_TENANT_OVERRIDE
    svc.validate_override_scope(slug, None)  # global override is fine


def test_tenant_override_of_regular_permission_allowed() -> None:
    svc.validate_override_scope(PRODUCTS_VIEW, uuid4())


@pytest.mark.parametrize(
    "expires_at",
    [NOW, NOW - timedelta(seconds=1), datetime(2030, 1, 1)],  # noqa: DTZ001
)
def test_expires_at_must_be_aware_future(expires_at: datetime) -> None:
    with pytest.raises(ValidationError) as info:
        svc.validate_expires_at(expires_at, NOW)
    assert info.value.code == svc.EXPIRES_AT_NOT_IN_FUTURE


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
    with pytest.raises(ValidationError):
        svc.validate_role_slug(slug)


@pytest.mark.parametrize("value", [0, -1, True])
def test_bad_telegram_id(value: int) -> None:
    with pytest.raises(ValidationError):
        svc.validate_telegram_user_id(value)


def test_names_and_usernames() -> None:
    assert svc.validate_name("  Shop ") == "Shop"
    with pytest.raises(ValidationError):
        svc.validate_name("   ")
    assert svc.normalize_username(" @john ") == "john"
    assert svc.normalize_username("@") is None
    assert svc.normalize_username(None) is None


def test_tenant_settings_validation() -> None:
    assert svc.validate_tenant_settings({"is_store_open": True, "support_chat_id": -100}) == {
        "is_store_open": True,
        "support_chat_id": -100,
    }
    for bad in (
        {"faq": None},
        {"is_store_open": 1},
        {"news_chat_id": True},
        {"default_language_code": ""},
    ):
        with pytest.raises(ValidationError):
            svc.validate_tenant_settings(bad)


def test_permission_slug_format() -> None:
    with pytest.raises(ValidationError):
        svc.validate_permission_slugs(["products.view", "bad"])


async def test_invalidation_runs_after_commit_and_retries() -> None:
    calls: list[str] = []
    session = AsyncMock()
    session.commit.side_effect = lambda: calls.append("commit")
    permissions = AsyncMock()
    results = iter([InvalidationResult(ok=False), InvalidationResult(ok=True)])

    async def invalidate(tg: int) -> InvalidationResult:
        calls.append(f"invalidate:{tg}")
        return next(results)

    permissions.invalidate.side_effect = invalidate
    service = svc.UserAdminService(session, permissions, audit=AsyncMock(), repository=AsyncMock())
    async with service._transaction():
        calls.append("work")
    await service._invalidate([7])
    assert calls == ["work", "commit", "invalidate:7", "invalidate:7"]


async def test_error_rolls_back_without_commit() -> None:
    session = AsyncMock()
    service = svc.UserAdminService(session, AsyncMock(), audit=AsyncMock(), repository=AsyncMock())
    with pytest.raises(ValidationError):
        async with service._transaction():
            raise ValidationError("x")
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
