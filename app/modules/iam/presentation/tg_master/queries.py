"""Read-only queries of the IAM dialogs (no writes: the admin services own the mutations)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam.models import (
    Language,
    Role,
    Tenant,
    TenantModerator,
    TenantSettings,
    User,
    UserRole,
)

__all__ = ["IamAdminQueries"]


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
        """Shops the user moderates (``tenant_moderators``)."""
        rows = await self._session.scalars(
            select(Tenant)
            .join(TenantModerator, TenantModerator.tenant_id == Tenant.id)
            .where(TenantModerator.user_id == user_id)
            .order_by(Tenant.name)
        )
        return list(rows)
