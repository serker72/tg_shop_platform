"""Write-side queries of the IAM admin services (no commit here - the services commit)."""

from __future__ import annotations

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

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

__all__ = ["IamAdminRepository"]


class IamAdminRepository:
    """Lookups and bulk changes used by :mod:`app.modules.iam.admin_service`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- users -----------------------------------------------------------------------

    async def get_user_by_telegram_id(
        self, telegram_user_id: int, *, include_deleted: bool = False, for_update: bool = False
    ) -> User | None:
        stmt = select(User).where(User.telegram_user_id == telegram_user_id)
        if not include_deleted:
            stmt = stmt.where(User.is_deleted.is_(False))
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.scalars(stmt)).one_or_none()

    async def delete_user_assignments(self, user_id: UUID) -> dict[str, int]:
        """Physically remove roles, overrides and shop assignments of the user."""
        counts: dict[str, int] = {}
        for name, model in (
            ("user_roles", UserRole),
            ("overrides", UserPermissionOverride),
            ("tenant_moderators", TenantModerator),
        ):
            ids = (
                await self._session.scalars(
                    delete(model).where(model.user_id == user_id).returning(model.id)
                )
            ).all()
            counts[name] = len(ids)
        return counts

    # --- tenants ---------------------------------------------------------------------

    async def get_tenant(
        self, tenant_id: UUID, *, include_deleted: bool = False, for_update: bool = False
    ) -> Tenant | None:
        stmt = select(Tenant).where(Tenant.id == tenant_id)
        if not include_deleted:
            stmt = stmt.where(Tenant.is_deleted.is_(False))
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.scalars(stmt)).one_or_none()

    async def tenant_slug_taken(self, slug: str) -> bool:
        """Soft-deleted tenants keep their slug (it is never reused)."""
        stmt = select(func.count()).select_from(Tenant).where(Tenant.slug == slug)
        return bool(await self._session.scalar(stmt))

    async def get_tenant_settings(
        self, tenant_id: UUID, *, for_update: bool = False
    ) -> TenantSettings | None:
        stmt = select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.scalars(stmt)).one_or_none()

    async def list_tenants(
        self, tenant_ids: Collection[UUID] | None = None, *, include_deleted: bool = False
    ) -> list[Tenant]:
        """All tenants (``tenant_ids=None``, ``tenants.view_all``) or only the given ones."""
        stmt = select(Tenant).order_by(Tenant.name, Tenant.id)
        if tenant_ids is not None:
            if not tenant_ids:
                return []
            stmt = stmt.where(Tenant.id.in_(list(tenant_ids)))
        if not include_deleted:
            stmt = stmt.where(Tenant.is_deleted.is_(False))
        return list((await self._session.scalars(stmt)).all())

    async def language_is_live(self, code: str) -> bool:
        stmt = (
            select(func.count())
            .select_from(Language)
            .where(Language.code == code, Language.is_active.is_(True))
            .where(Language.is_deleted.is_(False))
        )
        return bool(await self._session.scalar(stmt))

    # --- roles / permissions ----------------------------------------------------------

    async def get_role(
        self, role_id: UUID, *, include_deleted: bool = False, for_update: bool = False
    ) -> Role | None:
        stmt = select(Role).where(Role.id == role_id)
        if not include_deleted:
            stmt = stmt.where(Role.is_deleted.is_(False))
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.scalars(stmt)).one_or_none()

    async def role_slug_taken(self, slug: str) -> bool:
        stmt = select(func.count()).select_from(Role).where(Role.slug == slug)
        return bool(await self._session.scalar(stmt))

    async def live_permissions(self, slugs: Collection[str]) -> dict[str, Permission]:
        """Live (``is_active AND NOT is_deleted``) permissions among ``slugs``."""
        if not slugs:
            return {}
        stmt = select(Permission).where(
            Permission.slug.in_(list(slugs)),
            Permission.is_active.is_(True),
            Permission.is_deleted.is_(False),
        )
        return {p.slug: p for p in (await self._session.scalars(stmt)).all()}

    async def role_permission_slugs(self, role_id: UUID) -> frozenset[str]:
        stmt = (
            select(Permission.slug)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role_id)
        )
        return frozenset((await self._session.scalars(stmt)).all())

    async def replace_role_permissions(
        self, role_id: UUID, permission_ids: Collection[UUID]
    ) -> None:
        await self._session.execute(delete(RolePermission).where(RolePermission.role_id == role_id))
        self._session.add_all(
            RolePermission(role_id=role_id, permission_id=pid) for pid in permission_ids
        )
        await self._session.flush()

    # --- assignments ------------------------------------------------------------------

    async def get_user_role(self, user_id: UUID, role_id: UUID) -> UserRole | None:
        stmt = select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == role_id)
        return (await self._session.scalars(stmt)).one_or_none()

    async def get_override(
        self, user_id: UUID, permission_id: UUID, tenant_id: UUID | None
    ) -> UserPermissionOverride | None:
        """The (user, permission, tenant) slot - NULL tenant included - locked for update."""
        stmt = select(UserPermissionOverride).where(
            UserPermissionOverride.user_id == user_id,
            UserPermissionOverride.permission_id == permission_id,
            UserPermissionOverride.tenant_id.is_(None)
            if tenant_id is None
            else UserPermissionOverride.tenant_id == tenant_id,
        )
        return (await self._session.scalars(stmt.with_for_update())).one_or_none()

    async def get_tenant_moderator(self, user_id: UUID, tenant_id: UUID) -> TenantModerator | None:
        stmt = select(TenantModerator).where(
            TenantModerator.user_id == user_id, TenantModerator.tenant_id == tenant_id
        )
        return (await self._session.scalars(stmt)).one_or_none()
