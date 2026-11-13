"""Unit tests of :mod:`app.modules.iam.effective_permissions` (pure report assembly)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.modules.iam.effective_permissions import (
    EffectivePermissionsReport,
    OverrideRef,
    RoleRef,
    UserStatus,
    build_report,
)
from app.modules.iam.permissions import (
    ORDERS_VIEW,
    PRODUCTS_EDIT,
    PRODUCTS_VIEW,
    ROLE_MODERATOR,
    ROLE_SENIOR_MODERATOR,
    SEED_PERMISSION_SLUGS,
    TENANTS_VIEW_ALL,
)
from app.modules.iam.snapshot import DecisionSource, PermissionSnapshot

pytestmark = pytest.mark.req("R3")

TG = 4242
NOW = datetime(2030, 1, 1, tzinfo=UTC)
MODERATOR = RoleRef(slug=ROLE_MODERATOR, name="Moderator")


def report_for(
    snapshot: PermissionSnapshot,
    *,
    status: UserStatus = UserStatus.ACTIVE,
    roles: tuple[RoleRef, ...] = (),
    overrides: tuple[tuple[str, OverrideRef], ...] = (),
    expired: tuple[tuple[str, OverrideRef], ...] = (),
) -> EffectivePermissionsReport:
    return build_report(
        telegram_user_id=TG,
        snapshot=snapshot,
        status=status,
        roles=roles,
        overrides=overrides,
        expired_overrides=expired,
    )


def live_user(**kwargs: object) -> PermissionSnapshot:
    return PermissionSnapshot(user_id=uuid4(), **kwargs)  # type: ignore[arg-type]


def global_override(*, granted: bool, reason: str | None = None) -> OverrideRef:
    return OverrideRef(
        is_granted=granted,
        tenant_id=None,
        expires_at=None,
        granted_by_user_id=uuid4(),
        reason=reason,
    )


def test_catalogue_is_the_constant() -> None:
    """R-3-A: the report lists the IAM constant, not a ``SELECT`` on ``permissions``."""
    report = report_for(live_user())
    assert {item.permission_slug for item in report.permissions} == set(SEED_PERMISSION_SLUGS)
    assert {item.tenant_id for item in report.permissions} == {None}


def test_role_is_named_as_the_source() -> None:
    tenant = uuid4()
    report = report_for(
        live_user(role_permissions={PRODUCTS_VIEW: frozenset({ROLE_MODERATOR})}, tenants={tenant}),
        roles=(MODERATOR,),
    )
    entry = report.get(PRODUCTS_VIEW)
    assert entry is not None
    assert (entry.allowed, entry.source) == (True, DecisionSource.ROLE)
    assert entry.roles == (MODERATOR,)
    assert entry.override is None
    assert report.status is UserStatus.ACTIVE
    assert report.roles == (MODERATOR,)
    assert report.tenants == (tenant,)


def test_role_without_permissions_is_reported_but_names_nothing() -> None:
    """R-11: the assignment is visible, its permissions are not."""
    report = report_for(live_user(), roles=(MODERATOR,))
    assert report.roles == (MODERATOR,)
    entry = report.get(PRODUCTS_VIEW)
    assert entry is not None
    assert (entry.allowed, entry.source) == (False, DecisionSource.NOT_GRANTED)
    assert entry.roles == ()


def test_global_grant_override() -> None:
    row = global_override(granted=True, reason="temporary help")
    report = report_for(
        live_user(global_allow=frozenset({ORDERS_VIEW})), overrides=((ORDERS_VIEW, row),)
    )
    entry = report.get(ORDERS_VIEW)
    assert entry is not None
    assert (entry.allowed, entry.source) == (True, DecisionSource.OVERRIDE_ALLOW_GLOBAL)
    assert entry.override == row
    assert entry.roles == ()


def test_global_deny_override_beats_role() -> None:
    row = global_override(granted=False)
    report = report_for(
        live_user(
            role_permissions={PRODUCTS_VIEW: frozenset({ROLE_MODERATOR})},
            global_deny=frozenset({PRODUCTS_VIEW}),
        ),
        roles=(MODERATOR,),
        overrides=((PRODUCTS_VIEW, row),),
    )
    entry = report.get(PRODUCTS_VIEW)
    assert entry is not None
    assert (entry.allowed, entry.source) == (False, DecisionSource.OVERRIDE_DENY_GLOBAL)
    assert entry.override == row
    assert entry.roles == ()
    # The role itself is still reported: it exists, it is just overridden.
    assert report.roles == (MODERATOR,)


def test_tenant_record_keeps_the_global_override_that_decided_it() -> None:
    """R-4: a global override decides a tenant check too - the tenant entry shows it."""
    tenant = uuid4()
    row = global_override(granted=False, reason="no edits anywhere")
    report = report_for(
        live_user(global_deny=frozenset({PRODUCTS_EDIT}), tenants={tenant}),
        overrides=((PRODUCTS_EDIT, row),),
    )
    scoped = report.get(PRODUCTS_EDIT, tenant)
    assert scoped is not None
    assert (scoped.allowed, scoped.source) == (False, DecisionSource.OVERRIDE_DENY_GLOBAL)
    assert scoped.override == row
    assert scoped.tenant_id == tenant


def test_tenant_override_allows_only_in_its_shop() -> None:
    own, foreign = uuid4(), uuid4()
    row = OverrideRef(
        is_granted=True,
        tenant_id=own,
        expires_at=None,
        granted_by_user_id=uuid4(),
        reason="shop owner",
    )
    report = report_for(live_user(tenants=frozenset({own})), overrides=((PRODUCTS_EDIT, row),))
    assert report.tenants == (own,)
    scoped = report.get(PRODUCTS_EDIT, own)
    assert scoped is not None
    assert (scoped.allowed, scoped.source) == (True, DecisionSource.OVERRIDE_ALLOW_TENANT)
    assert scoped.override == row
    glob = report.get(PRODUCTS_EDIT)
    assert glob is not None
    assert (glob.allowed, glob.source) == (False, DecisionSource.NOT_GRANTED)
    assert report.get(PRODUCTS_EDIT, foreign) is None  # not a described shop


def test_tenant_override_on_an_unreachable_shop_is_described_but_denied() -> None:
    """R-1-A + RC-1: the shop is described, the answer still matches a real check.

    An override never widens ``has_tenant_access``: without ``tenant_moderators`` and
    without ``tenants.view_all`` the entry is ``allowed=False, source=no_tenant_access`` -
    exactly what ``PermissionSnapshot.decide`` returns for the same state.
    """
    shop = uuid4()
    row = OverrideRef(
        is_granted=True,
        tenant_id=shop,
        expires_at=None,
        granted_by_user_id=None,
        reason=None,
    )
    snapshot = live_user()
    report = report_for(snapshot, overrides=((PRODUCTS_EDIT, row),))
    assert report.tenants == (shop,)
    entry = report.get(PRODUCTS_EDIT, shop)
    assert entry is not None
    assert (entry.allowed, entry.source) == (False, DecisionSource.NO_TENANT_ACCESS)
    # The report and a live check agree (RC-1 invariant, unit level).
    decision = snapshot.decide(PRODUCTS_EDIT, shop)
    assert (entry.allowed, entry.source) == (decision.allowed, decision.source)


def test_tenant_override_of_a_reachable_shop_decides_the_pair() -> None:
    """With ``tenants.view_all`` the shop *is* reachable, so the override column decides."""
    shop = uuid4()
    row = OverrideRef(
        is_granted=True,
        tenant_id=shop,
        expires_at=None,
        granted_by_user_id=None,
        reason=None,
    )
    snapshot = live_user(role_permissions={TENANTS_VIEW_ALL: frozenset({ROLE_SENIOR_MODERATOR})})
    report = report_for(
        snapshot,
        roles=(RoleRef(slug=ROLE_SENIOR_MODERATOR, name="Senior moderator"),),
        overrides=((PRODUCTS_EDIT, row),),
    )
    entry = report.get(PRODUCTS_EDIT, shop)
    assert entry is not None
    assert (entry.allowed, entry.source) == (True, DecisionSource.OVERRIDE_ALLOW_TENANT)
    assert entry.override == row


def test_no_tenant_access_without_an_assignment() -> None:
    """A permissionless user denies the shop for the right reason, and ``tenants.view_all``
    never gets a per-shop duplicate."""
    shop = uuid4()
    report = report_for(live_user())
    assert report.tenants == ()
    assert report.get(PRODUCTS_VIEW, shop) is None

    assigned = report_for(live_user(tenants={shop}))
    # The shop is described, yet the user holds no permission there.
    entry = assigned.get(PRODUCTS_VIEW, shop)
    assert entry is not None
    assert (entry.allowed, entry.source) == (False, DecisionSource.NOT_GRANTED)
    slugs = {item.permission_slug for item in assigned.permissions if item.tenant_id == shop}
    assert "tenants.view_all" not in slugs


def test_superuser_is_a_single_wildcard_entry() -> None:
    """R-2: no per-(permission, shop) walk for a user who bypasses every check."""
    report = report_for(PermissionSnapshot(user_id=uuid4(), is_superuser=True))
    assert report.is_superuser is True
    assert report.status is UserStatus.ACTIVE
    assert len(report.permissions) == 1
    entry = report.permissions[0]
    assert (entry.permission_slug, entry.allowed, entry.source) == (
        "*",
        True,
        DecisionSource.SUPERUSER,
    )


def test_every_non_active_status_has_no_permissions() -> None:
    """R-5-A: ``unknown`` / ``inactive`` / ``deleted`` instead of a bare boolean."""
    for status in (UserStatus.UNKNOWN, UserStatus.INACTIVE, UserStatus.DELETED):
        report = report_for(PermissionSnapshot.missing(), status=status)
        assert report.status is status
        assert report.is_active is False
        assert report.is_superuser is False
        assert report.permissions == ()
        assert report.roles == () and report.tenants == ()
    assert report_for(PermissionSnapshot(user_id=uuid4())).is_active is True


def test_expired_overrides_go_to_a_separate_section() -> None:
    expired_row = OverrideRef(
        is_granted=True,
        tenant_id=None,
        expires_at=NOW - timedelta(seconds=1),
        granted_by_user_id=uuid4(),
        reason="ended",
    )
    report = report_for(live_user(), expired=((ORDERS_VIEW, expired_row),))
    live = report.get(ORDERS_VIEW)
    assert live is not None
    assert (live.allowed, live.source) == (False, DecisionSource.NOT_GRANTED)
    assert len(report.expired_overrides) == 1
    expired = report.expired_overrides[0]
    assert expired.permission_slug == ORDERS_VIEW
    assert expired.override == expired_row
    assert expired.allowed is True  # historical is_granted, not a live permission
    assert expired.source is DecisionSource.NOT_GRANTED


def test_expired_section_is_sorted() -> None:
    row = OverrideRef(
        is_granted=False,
        tenant_id=None,
        expires_at=NOW,
        granted_by_user_id=None,
        reason=None,
    )
    report = report_for(
        live_user(),
        expired=((PRODUCTS_VIEW, row), (ORDERS_VIEW, row)),
    )
    assert [item.permission_slug for item in report.expired_overrides] == [
        ORDERS_VIEW,
        PRODUCTS_VIEW,
    ]


def test_view_all_does_not_create_foreign_shop_entries() -> None:
    """R-1-A: ``tenants.view_all`` widens reach, it does not describe a shop."""
    own = uuid4()
    report = report_for(
        live_user(
            tenants={own},
            role_permissions={TENANTS_VIEW_ALL: frozenset({ROLE_SENIOR_MODERATOR})},
        ),
        roles=(RoleRef(slug=ROLE_SENIOR_MODERATOR, name="Senior moderator"),),
    )
    # Only the assignment is described; ``view_all`` grants no entry for other shops.
    assert report.tenants == (own,)
    assert {item.tenant_id for item in report.permissions} == {None, own}
    view_all = report.get(TENANTS_VIEW_ALL)
    assert view_all is not None and view_all.allowed is True
    foreign = uuid4()
    assert report.get(PRODUCTS_VIEW, foreign) is None
