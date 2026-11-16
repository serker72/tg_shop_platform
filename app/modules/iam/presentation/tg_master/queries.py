"""Read-only queries of the IAM dialogs (no writes: the admin services own the mutations)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.modules.iam.admin_repository import IamAdminRepository
from app.modules.iam.models import (
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

__all__ = ["IamAdminQueries", "OverrideRow"]


@dataclass(frozen=True, slots=True)
class OverrideRow:
    """One override of a user with the permission slug and the granting staff member."""

    override: UserPermissionOverride
    permission_slug: str
    granted_by_telegram_id: int | None
    granted_by_username: str | None
    is_expired: bool


class IamAdminQueries:
    """Plain SELECTs; loaded objects are never modified, so the session stays clean."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def tenant(self, tenant_id: UUID) -> tuple[Tenant, TenantSettings | None] | None:
        """Tenant (soft-deleted included) with its settings row."""
        tenant = await self._session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        if tenant is None:
            return None
        settings = await self._session.scalar(
            select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
        )
        return tenant, settings

    async def deleted_tenants(self) -> list[Tenant]:
        rows = await self._session.scalars(
            select(Tenant).where(Tenant.is_deleted.is_(True)).order_by(Tenant.name)
        )
        return list(rows)

    async def live_languages(self) -> list[Language]:
        rows = await self._session.scalars(
            select(Language)
            .where(Language.is_active.is_(True), Language.is_deleted.is_(False))
            .order_by(Language.code)
        )
        return list(rows)

    async def users(self) -> list[User]:
        """Staff users, soft-deleted included (they can be restored), live ones first."""
        rows = await self._session.scalars(
            select(User).order_by(User.is_deleted, User.telegram_user_id)
        )
        return list(rows)

    async def user(self, telegram_user_id: int) -> User | None:
        return await self._session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )

    async def user_roles(self, user_id: UUID) -> list[str]:
        """Names of the roles assigned to the user (role assignments are global)."""
        rows = await self._session.scalars(
            select(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
            .order_by(Role.name)
        )
        return list(rows)

    async def user_tenants(self, user_id: UUID) -> list[Tenant]:
        """Shops the user moderates (``tenant_moderators``), soft-deleted ones included:
        tenant soft-delete keeps the assignment rows; callers filter by the viewer's rights."""
        rows = await self._session.scalars(
            select(Tenant)
            .join(TenantModerator, TenantModerator.tenant_id == Tenant.id)
            .where(TenantModerator.user_id == user_id)
            .order_by(Tenant.name)
        )
        return list(rows)

    async def has_global_access(self, user: User) -> bool:
        """Superuser or a global ``tenants.view_all`` (live role / global allow, a global deny
        wins) - the same rule as ``AccessAdminService`` uses to refuse shop assignments."""
        if user.is_superuser:
            return True
        return await IamAdminRepository(self._session).holds_global_view_all(
            user.id, now=datetime.now(UTC)
        )

    # --- roles (task 06) ----------------------------------------------------------------

    async def roles(self) -> list[Role]:
        """Live and deactivated roles (soft-deleted ones are hidden), system roles first."""
        rows = await self._session.scalars(
            select(Role)
            .where(Role.is_deleted.is_(False))
            .order_by(Role.is_system.desc(), Role.name)
        )
        return list(rows)

    async def role(self, role_id: UUID) -> Role | None:
        return await self._session.scalar(
            select(Role).where(Role.id == role_id, Role.is_deleted.is_(False))
        )

    async def role_permission_slugs(self, role_id: UUID) -> frozenset[str]:
        """Slugs linked to the role (inactive permissions included: the stored set)."""
        rows = await self._session.scalars(
            select(Permission.slug)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role_id)
        )
        return frozenset(rows)

    async def user_role_ids(self, user_id: UUID) -> frozenset[UUID]:
        rows = await self._session.scalars(
            select(UserRole.role_id).where(UserRole.user_id == user_id)
        )
        return frozenset(rows)

    async def user_overrides(self, user_id: UUID) -> list[OverrideRow]:
        """Overrides of the user (expired ones flagged) with the granting staff member."""
        granter = aliased(User)
        now = datetime.now(UTC)
        result = await self._session.execute(
            select(
                UserPermissionOverride,
                Permission.slug,
                granter.telegram_user_id,
                granter.telegram_user_name,
            )
            .join(Permission, Permission.id == UserPermissionOverride.permission_id)
            .outerjoin(granter, granter.id == UserPermissionOverride.granted_by_id)
            .where(UserPermissionOverride.user_id == user_id)
            .order_by(Permission.slug, UserPermissionOverride.tenant_id.nulls_first())
        )
        return [
            OverrideRow(
                override=row,
                permission_slug=slug,
                granted_by_telegram_id=tg_id,
                granted_by_username=username,
                is_expired=row.expires_at is not None and row.expires_at <= now,
            )
            for row, slug, tg_id, username in result.all()
        ]

    async def user_brief(self, user_id: UUID) -> tuple[int, str | None] | None:
        """Telegram id and username of a user by the internal id (soft-deleted included)."""
        row = (
            await self._session.execute(
                select(User.telegram_user_id, User.telegram_user_name).where(User.id == user_id)
            )
        ).first()
        return None if row is None else (row[0], row[1])

    async def live_permission_slugs(self) -> frozenset[str]:
        """Slugs of live (``is_active AND NOT is_deleted``) permissions."""
        rows = await self._session.scalars(
            select(Permission.slug).where(
                Permission.is_active.is_(True), Permission.is_deleted.is_(False)
            )
        )
        return frozenset(rows)
