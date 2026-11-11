"""Effective permissions report of one user: why exactly they hold a permission.

TZ (line 1612): "определить, почему конкретный пользователь имеет конкретное разрешение:
через какую роль или через индивидуальное назначение". The report answers that question for
support and for the admin UI.

Design decisions baked in here:

* **PostgreSQL only, never the cache** (decision 2-a): an administrative report shows the
  committed state, not a possibly stale Redis entry held by another process.
* **``allowed`` comes from :meth:`PermissionSnapshot.decide`** - the very rule
  :class:`app.modules.iam.service.PermissionService` uses - so a report and a real
  permission check can never disagree.
* **The permission catalogue is the constant** from
  :mod:`app.modules.iam.permissions` (decision R-3-A), not a ``SELECT`` on ``permissions``:
  the report lists exactly the known slugs, so a disabled (``is_active = false``) or
  soft-deleted permission is *not* reported at all. Such a permission denies every check
  (``decide`` answers ``not_granted``) and an override pointing at it explains nothing -
  showing it would only make support chase a permission that cannot be granted. Overrides
  of disabled permissions are outside the report on purpose; the write service refuses to
  create them for a disabled permission.
* **Expired overrides** are kept out of ``permissions`` and listed in
  ``expired_overrides`` (decision 1-a): they no longer grant or deny anything, but support
  still needs to see that a temporary grant ran out.
* **Per-shop entries** (decision R-1-A) exist only for shops the user is assigned to
  (``tenant_moderators``) or has a tenant override in. ``tenants.view_all`` alone grants
  access to all shops but does **not** create entries for foreign shops: the report
  describes stated assignments, not the reach of a permission.
* **Invariant "the report never disagrees with a check"** (decision RC-1): every entry is
  produced from the access gate of :meth:`PermissionSnapshot.decide`. A tenant override on
  a shop the user cannot reach is described but reported as ``no_tenant_access`` - an
  override never widens ``has_tenant_access``, so the entry and a live check agree.
* **``status``** (decision R-5-A) tells *why* there are no permissions: ``unknown``,
  ``active``, ``inactive`` or ``deleted``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.modules.iam.permissions import GLOBAL_PERMISSIONS, SEED_PERMISSION_SLUGS
from app.modules.iam.snapshot import Decision, DecisionSource, PermissionSnapshot

__all__ = [
    "EffectivePermission",
    "EffectivePermissionsReport",
    "OverrideRef",
    "RoleRef",
    "UserStatus",
    "build_report",
]

#: Override sources: the decision was made by an individual assignment.
_OVERRIDE_SOURCES = frozenset(
    {
        DecisionSource.OVERRIDE_ALLOW_GLOBAL,
        DecisionSource.OVERRIDE_ALLOW_TENANT,
        DecisionSource.OVERRIDE_DENY_GLOBAL,
        DecisionSource.OVERRIDE_DENY_TENANT,
    }
)


class UserStatus(StrEnum):
    """Why the report has (no) permissions (decision R-5-A).

    ``UNKNOWN`` - no such Telegram id; ``ACTIVE`` - live user; ``INACTIVE`` - the row
    exists but ``is_active = false``; ``DELETED`` - soft-deleted row.
    """

    UNKNOWN = "unknown"
    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class RoleRef:
    """A role that grants a permission."""

    slug: str
    name: str


@dataclass(frozen=True, slots=True)
class OverrideRef:
    """An individual (per-user) override row.

    ``tenant_id`` is ``None`` for a global override; ``expires_at`` is ``None`` for a
    permanent one; ``granted_by_user_id`` / ``reason`` may be empty.
    """

    is_granted: bool
    tenant_id: UUID | None
    expires_at: datetime | None
    granted_by_user_id: UUID | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class EffectivePermission:
    """One ``(permission, tenant)`` pair of the report.

    Attributes:
        permission_slug: checked permission.
        tenant_id: shop of the check; ``None`` for a global check.
        allowed: result of :meth:`PermissionSnapshot.decide`.
        source: why it was allowed / denied (:class:`DecisionSource` value).
        roles: live roles granting the permission (only when ``source`` is ``role``).
        override: override that decided the outcome. A *tenant* entry keeps the override
            that decided it even when that override is global: ``source`` then says
            ``override_allow_global`` / ``override_deny_global`` while ``tenant_id`` is the
            shop that was asked about (decision R-4).
    """

    permission_slug: str
    tenant_id: UUID | None
    allowed: bool
    source: DecisionSource
    roles: tuple[RoleRef, ...]
    override: OverrideRef | None


@dataclass(frozen=True, slots=True)
class EffectivePermissionsReport:
    """Full picture of one user's rights.

    Attributes:
        telegram_user_id: requested Telegram user id.
        status: :class:`UserStatus` of the user.
        is_superuser: the user bypasses every check.
        roles: live roles assigned to the user (even roles without permissions).
        tenants: shops the report describes - assignments plus tenant overrides.
        permissions: global entry per known permission plus one per described shop. A
            superuser gets a single ``*`` entry with ``source = superuser``; a non-active
            user has no entries (there is nothing to decide for them).
        expired_overrides: overrides whose ``expires_at`` is in the past.
    """

    telegram_user_id: int
    status: UserStatus
    is_superuser: bool
    roles: tuple[RoleRef, ...]
    tenants: tuple[UUID, ...]
    permissions: tuple[EffectivePermission, ...]
    expired_overrides: tuple[EffectivePermission, ...]

    @property
    def is_active(self) -> bool:
        """``True`` when the user is live (``status is ACTIVE``)."""
        return self.status is UserStatus.ACTIVE

    def get(
        self, permission_slug: str, tenant_id: UUID | None = None
    ) -> EffectivePermission | None:
        """Entry of ``permissions`` for the pair, ``None`` when it is not in the report."""
        for item in self.permissions:
            if item.permission_slug == permission_slug and item.tenant_id == tenant_id:
                return item
        return None


def _roles(refs: tuple[RoleRef, ...], slugs: frozenset[str]) -> tuple[RoleRef, ...]:
    """Roles among ``refs`` whose slug is in ``slugs``, ordered by slug (stable output)."""
    known = {ref.slug: ref for ref in refs}
    return tuple(known[slug] for slug in sorted(slugs) if slug in known)


def _override_index(
    overrides: tuple[tuple[str, OverrideRef], ...],
) -> dict[tuple[str, UUID | None], OverrideRef]:
    """Index overrides by ``(slug, tenant_id)``; the database keeps one row per pair."""
    return {(slug, ref.tenant_id): ref for slug, ref in overrides}


def _override_for(
    index: dict[tuple[str, UUID | None], OverrideRef], slug: str, tenant_id: UUID | None
) -> OverrideRef | None:
    """Override that decided ``(slug, tenant)``.

    A tenant check may be decided by a *global* override (it beats every tenant scope), so
    the tenant key falls back to the global one (decision R-4).
    """
    return index.get((slug, tenant_id)) or index.get((slug, None))


def _expired_key(item: tuple[str, OverrideRef]) -> tuple[str, bool, str]:
    """Sort key of the expired section: slug, then global rows before shop rows."""
    slug, ref = item
    return slug, ref.tenant_id is not None, "" if ref.tenant_id is None else str(ref.tenant_id)


def _scoped_tenants(
    assigned: frozenset[UUID], overrides: tuple[tuple[str, OverrideRef], ...]
) -> tuple[UUID, ...]:
    """Shops described by the report: assignments plus shops with a tenant override.

    ``PermissionSnapshot.tenants`` already drops soft-deleted shops and so does the
    repository for overrides, hence a deleted shop never shows up here. ``tenants.view_all``
    is deliberately not added to the set (decision R-1-A): it widens the reach of a
    permission, it does not state an assignment.
    """
    with_override = {ref.tenant_id for _, ref in overrides if ref.tenant_id is not None}
    return tuple(sorted(assigned | with_override))


def _column_decision(snapshot: PermissionSnapshot, row: OverrideRef) -> Decision:
    """Decision of a tenant override in a shop the snapshot may reject up front.

    Decision RC-1: a report must never disagree with a real check, so the shop access gate
    is applied *first*, exactly as :meth:`PermissionSnapshot.decide` does. An override on a
    shop the user cannot reach (no ``tenant_moderators`` row and no ``tenants.view_all``)
    is therefore reported as ``allowed=False, source=no_tenant_access`` - the same answer a
    live check gives. A tenant override never widens ``has_tenant_access``.

    For a reachable shop ``decide`` would have short-circuited only because of
    ``tenants.view_all``; then the override column decides with the precedence ``decide``
    uses right after the access gate.
    """
    if row.tenant_id is not None and not snapshot.has_tenant_access(row.tenant_id):
        return Decision(allowed=False, source=DecisionSource.NO_TENANT_ACCESS)
    if snapshot.is_superuser:
        return Decision(allowed=True, source=DecisionSource.SUPERUSER)
    if row.is_granted:
        return Decision(allowed=True, source=DecisionSource.OVERRIDE_ALLOW_TENANT)
    return Decision(allowed=False, source=DecisionSource.OVERRIDE_DENY_TENANT)


def build_report(
    *,
    telegram_user_id: int,
    snapshot: PermissionSnapshot,
    status: UserStatus,
    roles: tuple[RoleRef, ...] = (),
    permissions: frozenset[str] = SEED_PERMISSION_SLUGS,
    overrides: tuple[tuple[str, OverrideRef], ...] = (),
    expired_overrides: tuple[tuple[str, OverrideRef], ...] = (),
) -> EffectivePermissionsReport:
    """Assemble the report from raw rows; ``allowed`` comes from ``snapshot.decide``.

    Args:
        telegram_user_id: requested user (kept even when unknown).
        snapshot: raw snapshot of the user, built from the *same* rows.
        status: lifecycle status of the user.
        roles: live roles assigned to the user.
        permissions: catalogue of known slugs (defaults to the IAM constant).
        overrides: ``(permission_slug, row)`` of live overrides (not expired).
        expired_overrides: ``(permission_slug, row)`` of overrides with ``expires_at <= now``.

    Returns:
        Report with a global entry per known permission plus one entry per described shop.
        A tenant entry is created for every permission except :data:`GLOBAL_PERMISSIONS`
        (a tenant override of a global permission is never created by the write service).
    """
    role_refs = tuple(sorted(roles, key=lambda ref: ref.slug))
    index = _override_index(overrides)

    if not snapshot.exists:
        # Nothing to decide for an unknown / inactive / deleted user: ``decide`` answers
        # ``unknown_user`` for every slug, so dozens of identical denials would carry no
        # data. One look at ``status`` tells the whole story.
        return EffectivePermissionsReport(
            telegram_user_id=telegram_user_id,
            status=status,
            is_superuser=False,
            roles=(),
            tenants=(),
            permissions=(),
            expired_overrides=(),
        )

    if snapshot.is_superuser:
        # Superuser bypasses every check: one decision instead of a per-pair walk.
        return EffectivePermissionsReport(
            telegram_user_id=telegram_user_id,
            status=status,
            is_superuser=True,
            roles=role_refs,
            tenants=(),
            permissions=(
                EffectivePermission(
                    permission_slug="*",
                    tenant_id=None,
                    allowed=True,
                    source=DecisionSource.SUPERUSER,
                    roles=(),
                    override=None,
                ),
            ),
            expired_overrides=(),
        )

    # Shops described by the report (R-1-A): assignments plus shops with a tenant override.
    tenants = _scoped_tenants(snapshot.tenants, overrides)
    live = tuple(sorted(permissions))
    scoped = tuple(slug for slug in live if slug not in GLOBAL_PERMISSIONS)
    # An override column of a shop without an assignment still decides that pair (R-1-A).
    override_columns = {
        (slug, ref.tenant_id)
        for slug, ref in overrides
        if ref.tenant_id is not None and slug not in GLOBAL_PERMISSIONS
    }

    def entry(slug: str, tenant_id: UUID | None) -> EffectivePermission:
        if tenant_id is None:
            decision = snapshot.decide(slug)
        elif (slug, tenant_id) in override_columns:
            decision = _column_decision(snapshot, index[(slug, tenant_id)])
        else:
            decision = snapshot.decide(slug, tenant_id)
        source = decision.source
        # Roles are named only when they actually participate in the decision.
        granted_roles = _roles(role_refs, decision.roles) if source is DecisionSource.ROLE else ()
        override = _override_for(index, slug, tenant_id) if source in _OVERRIDE_SOURCES else None
        return EffectivePermission(
            permission_slug=slug,
            tenant_id=tenant_id,
            allowed=decision.allowed,
            source=source,
            roles=granted_roles,
            override=override,
        )

    return EffectivePermissionsReport(
        telegram_user_id=telegram_user_id,
        status=status,
        is_superuser=False,
        roles=role_refs,
        tenants=tenants,
        permissions=(
            *(entry(slug, None) for slug in live),
            *(entry(slug, tenant_id) for slug in scoped for tenant_id in tenants),
        ),
        expired_overrides=tuple(
            EffectivePermission(
                permission_slug=slug,
                tenant_id=ref.tenant_id,
                # Historical ``is_granted``: what the grant used to do, not a live right.
                allowed=ref.is_granted,
                source=DecisionSource.NOT_GRANTED,
                roles=(),
                override=ref,
            )
            for slug, ref in sorted(expired_overrides, key=_expired_key)
        ),
    )
