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
