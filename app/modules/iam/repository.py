"""Read-only queries of the permission resolver (explicit columns, no ORM objects).

Liveness filters:

* users, roles, permissions: ``is_active AND NOT is_deleted``;
* overrides: ``expires_at IS NULL OR expires_at > :now``;
* shops (tenant overrides, ``tenant_moderators``): only ``NOT tenants.is_deleted``. A
  disabled shop (``is_active = false``) stays manageable by its moderators - switching a
  shop off must not lock its staff out of its settings.

Only ``SELECT``s; the caller owns the transaction (nothing is committed).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam.effective_permissions import (
    EffectivePermissionsReport,
    OverrideRef,
    RoleRef,
    UserStatus,
    build_report,
)
from app.modules.iam.models import (
    Permission,
    Role,
    RolePermission,
    Tenant,
    TenantModerator,
    User,
    UserPermissionOverride,
    UserRole,
)
from app.modules.iam.snapshot import PermissionSnapshot

__all__ = ["PermissionQueryRepository"]


class PermissionQueryRepository:
    """Loads :class:`PermissionSnapshot` from PostgreSQL (four small queries at most)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_snapshot(self, telegram_user_id: int, *, now: datetime) -> PermissionSnapshot:
        """Snapshot of a live user, :meth:`PermissionSnapshot.missing` otherwise.

        ``now`` must be timezone-aware (compared with ``expires_at timestamptz``).
        """
        user = (
            await self._session.execute(
                select(User.id, User.is_superuser).where(
                    User.telegram_user_id == telegram_user_id,
                    User.is_active.is_(True),
                    User.is_deleted.is_(False),
                )
            )
        ).one_or_none()
        if user is None:
            return PermissionSnapshot.missing()
        user_id, is_superuser = user
        if is_superuser:
            return PermissionSnapshot(user_id=user_id, is_superuser=True)

        role_permissions: defaultdict[str, set[str]] = defaultdict(set)
        rows = await self._session.execute(
            select(Permission.slug, Role.slug)
            .select_from(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(
                UserRole.user_id == user_id,
                Role.is_active.is_(True),
                Role.is_deleted.is_(False),
                Permission.is_active.is_(True),
                Permission.is_deleted.is_(False),
            )
        )
        for permission_slug, role_slug in rows:
            role_permissions[permission_slug].add(role_slug)

        override = UserPermissionOverride
        global_allow: set[str] = set()
        global_deny: set[str] = set()
        tenant_allow: defaultdict[UUID, set[str]] = defaultdict(set)
        tenant_deny: defaultdict[UUID, set[str]] = defaultdict(set)
        next_expiry: datetime | None = None
        override_rows = await self._session.execute(
            select(Permission.slug, override.tenant_id, override.is_granted, override.expires_at)
            .select_from(override)
            .join(Permission, Permission.id == override.permission_id)
            .outerjoin(Tenant, Tenant.id == override.tenant_id)
            .where(
                override.user_id == user_id,
                Permission.is_active.is_(True),
                Permission.is_deleted.is_(False),
                or_(override.expires_at.is_(None), override.expires_at > now),
                or_(override.tenant_id.is_(None), Tenant.is_deleted.is_(False)),
            )
        )
        for slug, tenant_id, is_granted, expires_at in override_rows:
            if tenant_id is None:
                (global_allow if is_granted else global_deny).add(slug)
            else:
                (tenant_allow if is_granted else tenant_deny)[tenant_id].add(slug)
            if expires_at is not None and (next_expiry is None or expires_at < next_expiry):
                next_expiry = expires_at

        tenants = (
            await self._session.scalars(
                select(TenantModerator.tenant_id)
                .join(Tenant, Tenant.id == TenantModerator.tenant_id)
                .where(TenantModerator.user_id == user_id, Tenant.is_deleted.is_(False))
            )
        ).all()

        return PermissionSnapshot(
            user_id=user_id,
            role_permissions={slug: frozenset(roles) for slug, roles in role_permissions.items()},
            global_allow=frozenset(global_allow),
            global_deny=frozenset(global_deny),
            tenant_allow={tid: frozenset(slugs) for tid, slugs in tenant_allow.items()},
            tenant_deny={tid: frozenset(slugs) for tid, slugs in tenant_deny.items()},
            tenants=frozenset(tenants),
            next_expiry=next_expiry,
        )

    async def load_effective_permissions(
        self, telegram_user_id: int, *, now: datetime
    ) -> EffectivePermissionsReport:
        """Why the user holds each permission: roles, overrides, expired overrides.

        PostgreSQL only, never the cache (decision 2-a): an administrative report must show
        the committed state. At most three reads of raw rows drive it:

        0. the user row itself - one read that also yields ``status`` (decision R-5-A);
        1. live roles of the user (``user_roles`` -> ``roles``, with ``granted_by_id``)
           joined with the permissions they grant (``role_permissions`` -> ``permissions``);
           an outer join keeps a role without permissions in the report;
        2. every override of the user with its permission and the granting user,
           *expired ones included* - they are split into ``expired_overrides`` afterwards.

        The permission catalogue is the :data:`SEED_PERMISSION_SLUGS` constant, not a
        ``SELECT`` (decision R-3-A); it is shared with the seed migration and verified by
        the migration test. A permission disabled in the database therefore simply stays
        out of the report - an override pointing at it explains nothing, because ``decide``
        denies a slug that no live role and no live override grants.

        A superuser returns after the user row: the snapshot answers ``superuser`` for every
        permission, so per-row reads would be wasted.

        Liveness: roles ``is_active AND NOT is_deleted``; a tenant override needs a shop with
        ``NOT tenants.is_deleted`` (a soft-deleted shop hides its overrides, as in the
        snapshot). Live data is fed into :class:`PermissionSnapshot` and every ``allowed``
        value comes from :meth:`PermissionSnapshot.decide`, so the report and a real check
        never disagree.
        """
        user_row = (
            await self._session.execute(
                select(User.id, User.is_superuser, User.is_active, User.is_deleted).where(
                    User.telegram_user_id == telegram_user_id
                )
            )
        ).one_or_none()
        if user_row is None:
            return build_report(
                telegram_user_id=telegram_user_id,
                snapshot=PermissionSnapshot.missing(),
                status=UserStatus.UNKNOWN,
            )
        user_id, is_superuser, is_active, is_deleted = user_row
        status = (
            UserStatus.DELETED
            if is_deleted
            else UserStatus.INACTIVE
            if not is_active
            else UserStatus.ACTIVE
        )
        if status is not UserStatus.ACTIVE:
            # The snapshot of a non-live user decides ``unknown_user``; no need to read
            # roles and overrides of a user who cannot act.
            return build_report(
                telegram_user_id=telegram_user_id,
                snapshot=PermissionSnapshot.missing(),
                status=status,
            )
        if is_superuser:
            return build_report(
                telegram_user_id=telegram_user_id,
                snapshot=PermissionSnapshot(user_id=user_id, is_superuser=True),
                status=status,
            )

        role_permissions: defaultdict[str, set[str]] = defaultdict(set)
        roles: dict[str, RoleRef] = {}
        role_rows = await self._session.execute(
            select(Role.slug, Role.name, Permission.slug)
            .select_from(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .outerjoin(RolePermission, RolePermission.role_id == Role.id)
            .outerjoin(
                Permission,
                (Permission.id == RolePermission.permission_id)
                & Permission.is_active.is_(True)
                & Permission.is_deleted.is_(False),
            )
            .where(
                UserRole.user_id == user_id,
                Role.is_active.is_(True),
                Role.is_deleted.is_(False),
            )
        )
        for role_slug, role_name, permission_slug in role_rows:
            roles[role_slug] = RoleRef(slug=role_slug, name=role_name)
            if permission_slug is not None:
                role_permissions[permission_slug].add(role_slug)

        override = UserPermissionOverride
        live_overrides: list[tuple[str, OverrideRef]] = []
        expired: list[tuple[str, OverrideRef]] = []
        global_allow: set[str] = set()
        global_deny: set[str] = set()
        tenant_allow: defaultdict[UUID, set[str]] = defaultdict(set)
        tenant_deny: defaultdict[UUID, set[str]] = defaultdict(set)
        next_expiry: datetime | None = None
        override_rows = await self._session.execute(
            select(
                Permission.slug,
                override.tenant_id,
                override.is_granted,
                override.expires_at,
                override.reason,
                override.granted_by_id,
            )
            .select_from(override)
            .join(Permission, Permission.id == override.permission_id)
            .outerjoin(Tenant, Tenant.id == override.tenant_id)
            .where(
                override.user_id == user_id,
                Permission.is_active.is_(True),
                Permission.is_deleted.is_(False),
                or_(override.tenant_id.is_(None), Tenant.is_deleted.is_(False)),
            )
            .order_by(Permission.slug)
        )
        for slug, tenant_id, is_granted, expires_at, reason, granted_by_id in override_rows:
            ref = OverrideRef(
                is_granted=is_granted,
                tenant_id=tenant_id,
                expires_at=expires_at,
                granted_by_user_id=granted_by_id,
                reason=reason,
            )
            if expires_at is not None and expires_at <= now:
                expired.append((slug, ref))
                continue
            live_overrides.append((slug, ref))
            if tenant_id is None:
                (global_allow if is_granted else global_deny).add(slug)
            else:
                (tenant_allow if is_granted else tenant_deny)[tenant_id].add(slug)
            if expires_at is not None and (next_expiry is None or expires_at < next_expiry):
                next_expiry = expires_at

        tenants = (
            await self._session.scalars(
                select(TenantModerator.tenant_id)
                .join(Tenant, Tenant.id == TenantModerator.tenant_id)
                .where(TenantModerator.user_id == user_id, Tenant.is_deleted.is_(False))
            )
        ).all()

        snapshot = PermissionSnapshot(
            user_id=user_id,
            role_permissions={slug: frozenset(slugs) for slug, slugs in role_permissions.items()},
            global_allow=frozenset(global_allow),
            global_deny=frozenset(global_deny),
            tenant_allow={tid: frozenset(slugs) for tid, slugs in tenant_allow.items()},
            tenant_deny={tid: frozenset(slugs) for tid, slugs in tenant_deny.items()},
            tenants=frozenset(tenants),
            next_expiry=next_expiry,
        )
        return build_report(
            telegram_user_id=telegram_user_id,
            snapshot=snapshot,
            status=status,
            roles=tuple(roles.values()),
            overrides=tuple(live_overrides),
            expired_overrides=tuple(expired),
        )
