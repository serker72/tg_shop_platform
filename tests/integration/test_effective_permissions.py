"""Effective permissions report against real PostgreSQL (savepoint session of the test DB).

The report is a pure PostgreSQL read (decision 2-a): Redis of the test stack is not used
here at all, the cache another request may hold must not influence it.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam.effective_permissions import UserStatus
from app.modules.iam.models import (
    Permission,
    Role,
    Tenant,
    TenantModerator,
    User,
    UserPermissionOverride,
    UserRole,
)
from app.modules.iam.permissions import (
    ORDERS_VIEW,
    PRODUCTS_EDIT,
    PRODUCTS_VIEW,
    ROLE_MODERATOR,
    ROLE_SENIOR_MODERATOR,
    TENANTS_VIEW_ALL,
)
from app.modules.iam.repository import PermissionQueryRepository
from app.modules.iam.snapshot import DecisionSource

pytestmark = pytest.mark.integration


#: Unique per test run and per call: the savepoint session rolls the rows back, but a flaky
#: collision on a random id would still fail an unrelated test.
_TELEGRAM_IDS = itertools.count(1_000_000_000)


def tg_id() -> int:
    return next(_TELEGRAM_IDS)


async def make_tenant(session: AsyncSession, *, name: str = "Shop") -> Tenant:
    tenant = Tenant(name=name, slug=f"shop-{uuid.uuid4().hex[:10]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def make_user(session: AsyncSession, *, superuser: bool = False) -> User:
    user = User(telegram_user_id=tg_id(), is_superuser=superuser)
    session.add(user)
    await session.flush()
    return user


async def grant_role(session: AsyncSession, user: User, slug: str, *, granted_by: User) -> None:
    role = (await session.scalars(select(Role).where(Role.slug == slug))).one()
    session.add(UserRole(user_id=user.id, role_id=role.id, granted_by_id=granted_by.id))
    await session.flush()


async def moderate(session: AsyncSession, user: User, tenant: Tenant) -> None:
    session.add(TenantModerator(user_id=user.id, tenant_id=tenant.id))
    await session.flush()


async def override(
    session: AsyncSession,
    user: User,
    slug: str,
    *,
    granted: bool,
    granted_by: User | None = None,
    tenant: Tenant | None = None,
    expires_at: datetime | None = None,
    reason: str | None = None,
) -> None:
    permission = (await session.scalars(select(Permission).where(Permission.slug == slug))).one()
    session.add(
        UserPermissionOverride(
            user_id=user.id,
            permission_id=permission.id,
            tenant_id=None if tenant is None else tenant.id,
            is_granted=granted,
            expires_at=expires_at,
            granted_by_id=None if granted_by is None else granted_by.id,
            reason=reason,
        )
    )
    await session.flush()


async def load(session: AsyncSession, user: User):  # type: ignore[no-untyped-def]
    return await PermissionQueryRepository(session).load_effective_permissions(
        user.telegram_user_id, now=datetime.now(UTC)
    )


async def test_unknown_user_has_empty_denied_report(session: AsyncSession) -> None:
    report = await PermissionQueryRepository(session).load_effective_permissions(
        tg_id(), now=datetime.now(UTC)
    )
    assert report.status is UserStatus.UNKNOWN
    assert report.is_superuser is False
    assert report.roles == () and report.tenants == ()
    assert report.permissions == () and report.expired_overrides == ()


async def test_role_source_names_the_role(session: AsyncSession) -> None:
    admin = await make_user(session)
    user = await make_user(session)
    tenant = await make_tenant(session)
    await grant_role(session, user, ROLE_MODERATOR, granted_by=admin)
    await moderate(session, user, tenant)

    report = await load(session, user)
    assert report.status is UserStatus.ACTIVE
    assert [ref.slug for ref in report.roles] == [ROLE_MODERATOR]
    assert report.tenants == (tenant.id,)

    entry = report.get(PRODUCTS_VIEW, tenant.id)
    assert entry is not None
    assert (entry.allowed, entry.source) == (True, DecisionSource.ROLE)
    assert [ref.slug for ref in entry.roles] == [ROLE_MODERATOR]
    assert entry.override is None

    # A shop the user does not moderate is not part of the report.
    assert report.get(PRODUCTS_VIEW, uuid4()) is None
    # Global check: the role grants it, no tenant needed.
    global_entry = report.get(PRODUCTS_VIEW)
    assert global_entry is not None and global_entry.allowed is True


async def test_override_allow_and_deny_are_reported(session: AsyncSession) -> None:
    admin = await make_user(session)
    user = await make_user(session)
    await override(session, user, ORDERS_VIEW, granted=True, granted_by=admin, reason="help out")
    await override(session, user, PRODUCTS_EDIT, granted=False, granted_by=admin, reason="no edits")

    report = await load(session, user)
    allowed = report.get(ORDERS_VIEW)
    assert allowed is not None
    assert (allowed.allowed, allowed.source) == (True, DecisionSource.OVERRIDE_ALLOW_GLOBAL)
    assert allowed.override is not None
    assert allowed.override.granted_by_user_id == admin.id
    assert allowed.override.reason == "help out"
    assert allowed.roles == ()

    denied = report.get(PRODUCTS_EDIT)
    assert denied is not None
    assert (denied.allowed, denied.source) == (False, DecisionSource.OVERRIDE_DENY_GLOBAL)
    assert denied.override is not None and denied.override.is_granted is False


async def test_tenant_override_is_scoped(session: AsyncSession) -> None:
    admin = await make_user(session)
    user = await make_user(session)
    shop = await make_tenant(session)
    await moderate(session, user, shop)
    await override(session, user, PRODUCTS_EDIT, granted=True, granted_by=admin, tenant=shop)

    report = await load(session, user)
    scoped = report.get(PRODUCTS_EDIT, shop.id)
    assert scoped is not None
    assert (scoped.allowed, scoped.source) == (True, DecisionSource.OVERRIDE_ALLOW_TENANT)
    assert scoped.override is not None and scoped.override.tenant_id == shop.id

    glob = report.get(PRODUCTS_EDIT)
    assert glob is not None
    assert (glob.allowed, glob.source) == (False, DecisionSource.NOT_GRANTED)
    # Global permissions never get a tenant entry.
    slugs_with_tenant = {item.permission_slug for item in report.permissions if item.tenant_id}
    assert TENANTS_VIEW_ALL not in slugs_with_tenant


async def test_expired_override_lands_in_its_own_section(session: AsyncSession) -> None:
    admin = await make_user(session)
    user = await make_user(session)
    await override(
        session,
        user,
        ORDERS_VIEW,
        granted=True,
        granted_by=admin,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        reason="expired grant",
    )

    report = await load(session, user)
    live = report.get(ORDERS_VIEW)
    assert live is not None
    assert (live.allowed, live.source) == (False, DecisionSource.NOT_GRANTED)
    assert live.override is None
    assert len(report.expired_overrides) == 1
    expired = report.expired_overrides[0]
    assert expired.permission_slug == ORDERS_VIEW
    assert expired.allowed is True  # what the grant used to do, not a live permission
    assert expired.source is DecisionSource.NOT_GRANTED
    assert expired.override is not None and expired.override.reason == "expired grant"
    assert expired.override.granted_by_user_id == admin.id


async def test_future_expiry_stays_live(session: AsyncSession) -> None:
    user = await make_user(session)
    await override(
        session,
        user,
        ORDERS_VIEW,
        granted=True,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    report = await load(session, user)
    entry = report.get(ORDERS_VIEW)
    assert entry is not None and entry.allowed is True
    assert report.expired_overrides == ()


async def test_superuser_report(session: AsyncSession) -> None:
    user = await make_user(session, superuser=True)
    report = await load(session, user)
    assert report.is_superuser is True and report.status is UserStatus.ACTIVE
    assert report.permissions
    assert len(report.permissions) == 1
    entry = report.permissions[0]
    assert (entry.permission_slug, entry.allowed, entry.source) == (
        "*",
        True,
        DecisionSource.SUPERUSER,
    )


async def test_soft_deleted_shop_hides_its_override(session: AsyncSession) -> None:
    admin = await make_user(session)
    user = await make_user(session)
    shop = await make_tenant(session)
    await moderate(session, user, shop)
    await override(session, user, PRODUCTS_EDIT, granted=True, granted_by=admin, tenant=shop)
    shop.is_deleted = True
    await session.flush()

    report = await load(session, user)
    assert report.tenants == ()
    assert all(item.tenant_id is None for item in report.permissions)
    entry = report.get(PRODUCTS_EDIT)
    assert entry is not None and entry.allowed is False


async def test_inactive_role_is_ignored(session: AsyncSession) -> None:
    admin = await make_user(session)
    user = await make_user(session)
    custom = Role(slug=f"custom_{uuid.uuid4().hex[:8]}", name="Custom")
    session.add(custom)
    await session.flush()
    session.add(UserRole(user_id=user.id, role_id=custom.id, granted_by_id=admin.id))
    await session.flush()
    custom.is_active = False
    await session.flush()

    report = await load(session, user)
    assert report.roles == ()
    entry = report.get(PRODUCTS_VIEW)
    assert entry is not None and entry.allowed is False


async def test_report_agrees_with_decide_on_every_entry(session: AsyncSession) -> None:
    """RC-1 invariant: every entry equals what a live check answers for the same state.

    Covers the awkward combination on purpose: a tenant override placed on a shop the user
    is *not* assigned to and holds no ``tenants.view_all`` for. The report describes such a
    shop (R-1-A) but must still answer ``no_tenant_access`` - an override never widens shop
    access - so ``allowed``/``source`` match ``decide`` for every single entry.
    """
    admin = await make_user(session)
    user = await make_user(session)
    assigned = await make_tenant(session, name="Assigned")
    override_only = await make_tenant(session, name="Override only")
    await grant_role(session, user, ROLE_MODERATOR, granted_by=admin)
    await moderate(session, user, assigned)
    await override(session, user, PRODUCTS_EDIT, granted=False, granted_by=admin)
    await override(
        session, user, PRODUCTS_VIEW, granted=True, granted_by=admin, tenant=override_only
    )

    snapshot = await PermissionQueryRepository(session).load_snapshot(
        user.telegram_user_id, now=datetime.now(UTC)
    )
    report = await load(session, user)
    assert report.permissions
    # The unreachable shop is present in the report, so the loop really exercises it.
    assert override_only.id in report.tenants
    assert report.get(PRODUCTS_VIEW, override_only.id) is not None
    for item in report.permissions:
        decision = snapshot.decide(item.permission_slug, item.tenant_id)
        assert (item.allowed, item.source) == (decision.allowed, decision.source), item


async def test_user_status_reports_why_there_are_no_permissions(session: AsyncSession) -> None:
    """R-5-A: ``unknown`` / ``inactive`` / ``deleted`` instead of a bare ``is_active``."""
    inactive = await make_user(session)
    inactive.is_active = False
    deleted = await make_user(session)
    deleted.is_deleted = True
    await session.flush()

    for user, expected in (
        (inactive, UserStatus.INACTIVE),
        (deleted, UserStatus.DELETED),
    ):
        report = await load(session, user)
        assert report.status is expected
        assert report.is_active is False
        assert report.permissions == ()

    unknown = await PermissionQueryRepository(session).load_effective_permissions(
        tg_id(), now=datetime.now(UTC)
    )
    assert unknown.status is UserStatus.UNKNOWN


async def test_view_all_does_not_describe_foreign_shops(session: AsyncSession) -> None:
    """R-1-A: ``tenants.view_all`` + a moderator role grants no entry for foreign shops."""
    admin = await make_user(session)
    user = await make_user(session)
    own = await make_tenant(session, name="Own")
    foreign = await make_tenant(session, name="Foreign")
    await grant_role(session, user, ROLE_SENIOR_MODERATOR, granted_by=admin)
    await moderate(session, user, own)

    report = await load(session, user)
    assert report.tenants == (own.id,)
    assert ROLE_SENIOR_MODERATOR in [ref.slug for ref in report.roles]
    # view_all is granted globally...
    view_all = report.get(TENANTS_VIEW_ALL)
    assert view_all is not None and view_all.allowed is True
    # ...but no other shop is described.
    assert report.get(PRODUCTS_VIEW, foreign.id) is None
    assert {item.tenant_id for item in report.permissions} == {None, own.id}


async def test_tenant_override_without_assignment_is_still_explained(
    session: AsyncSession,
) -> None:
    """R-1-A + RC-1: the shop is described, but an override never widens shop access."""
    admin = await make_user(session)
    user = await make_user(session)
    shop = await make_tenant(session)
    await override(session, user, PRODUCTS_EDIT, granted=True, granted_by=admin, tenant=shop)

    report = await load(session, user)
    assert report.tenants == (shop.id,)
    entry = report.get(PRODUCTS_EDIT, shop.id)
    assert entry is not None
    assert (entry.allowed, entry.source) == (False, DecisionSource.NO_TENANT_ACCESS)
    # An override is reported as the reason only when it actually decided something;
    # here the shop access gate refused first, exactly like ``decide`` does.
    assert entry.override is None
    snapshot = await PermissionQueryRepository(session).load_snapshot(
        user.telegram_user_id, now=datetime.now(UTC)
    )
    decision = snapshot.decide(PRODUCTS_EDIT, shop.id)
    assert (entry.allowed, entry.source) == (decision.allowed, decision.source)
