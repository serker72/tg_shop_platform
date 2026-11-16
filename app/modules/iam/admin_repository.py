"""Write-side queries of the IAM admin services (no commit here - the services commit)."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Select, delete, func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningInsert

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

__all__ = ["OVERRIDE_SLOT_CONSTRAINT", "IamAdminRepository", "build_override_upsert"]

#: ``UNIQUE NULLS NOT DISTINCT (user_id, permission_id, tenant_id)`` of the ``iam_rbac`` revision.
OVERRIDE_SLOT_CONSTRAINT = "uq_user_permission_overrides_user_id_permission_id_tenant_id"


def _locked[S: Select[Any]](stmt: S) -> S:
    """``FOR UPDATE`` + ``populate_existing``: the locked row overwrites a stale object of
    the identity map (``expire_on_commit=False`` keeps objects of earlier transactions)."""
    return stmt.with_for_update().execution_options(populate_existing=True)


def build_override_upsert(
    *,
    user_id: UUID,
    permission_id: UUID,
    tenant_id: UUID | None,
    is_granted: bool,
    expires_at: datetime | None,
    reason: str | None,
    granted_by_id: UUID | None,
) -> ReturningInsert[UserPermissionOverride, bool]:
    """``INSERT ... ON CONFLICT ON CONSTRAINT <slot> DO UPDATE RETURNING row, inserted``.

    ``inserted`` is ``xmax = 0``: ``true`` for a fresh row, ``false`` when the conflict
    branch updated an existing one (PostgreSQL sets ``xmax`` on the updated tuple).
    """
    stmt = insert(UserPermissionOverride).values(
        id=uuid4(),
        user_id=user_id,
        permission_id=permission_id,
        tenant_id=tenant_id,
        is_granted=is_granted,
        expires_at=expires_at,
        reason=reason,
        granted_by_id=granted_by_id,
    )
    return stmt.on_conflict_do_update(
        constraint=OVERRIDE_SLOT_CONSTRAINT,
        set_={
            "is_granted": stmt.excluded.is_granted,
            "expires_at": stmt.excluded.expires_at,
            "reason": stmt.excluded.reason,
            "granted_by_id": stmt.excluded.granted_by_id,
            "updated_at": func.now(),
        },
    ).returning(UserPermissionOverride, literal_column("(xmax = 0)", Boolean).label("inserted"))


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
            stmt = _locked(stmt)
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

    async def user_granted_permissions(
        self, user_id: UUID, *, now: datetime
    ) -> set[tuple[str, UUID | None]]:
        """Permissions the user effectively holds through grants (what activation returns).

        Live role permissions (global) + live allow-overrides (global, or in a shop that is
        not soft-deleted), minus slugs under a live global deny of the user (deny wins in
        ``PermissionSnapshot.decide``).
        """
        live_perm = (Permission.is_active.is_(True), Permission.is_deleted.is_(False))
        not_expired = or_(
            UserPermissionOverride.expires_at.is_(None), UserPermissionOverride.expires_at > now
        )
        deny_stmt = (
            select(Permission.slug)
            .join(UserPermissionOverride, UserPermissionOverride.permission_id == Permission.id)
            .where(
                UserPermissionOverride.user_id == user_id,
                UserPermissionOverride.is_granted.is_(False),
                UserPermissionOverride.tenant_id.is_(None),
                not_expired,
            )
        )
        denied = set((await self._session.scalars(deny_stmt)).all())
        role_stmt = (
            select(Permission.slug)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id, Role.is_active.is_(True))
            .where(Role.is_deleted.is_(False), *live_perm)
        )
        result: set[tuple[str, UUID | None]] = {
            (slug, None) for slug in (await self._session.scalars(role_stmt)).all()
        }
        override_stmt = (
            select(Permission.slug, UserPermissionOverride.tenant_id)
            .join(Permission, Permission.id == UserPermissionOverride.permission_id)
            .outerjoin(Tenant, Tenant.id == UserPermissionOverride.tenant_id)
            .where(
                UserPermissionOverride.user_id == user_id,
                UserPermissionOverride.is_granted.is_(True),
                not_expired,
                or_(UserPermissionOverride.tenant_id.is_(None), Tenant.is_deleted.is_(False)),
                *live_perm,
            )
        )
        result.update((row[0], row[1]) for row in (await self._session.execute(override_stmt)))
        return {item for item in result if item[0] not in denied}

    # --- tenants ---------------------------------------------------------------------

    async def get_tenant(
        self, tenant_id: UUID, *, include_deleted: bool = False, for_update: bool = False
    ) -> Tenant | None:
        stmt = select(Tenant).where(Tenant.id == tenant_id)
        if not include_deleted:
            stmt = stmt.where(Tenant.is_deleted.is_(False))
        if for_update:
            stmt = _locked(stmt)
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
            stmt = _locked(stmt)
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
            stmt = _locked(stmt)
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

    async def get_permission_by_slug(self, slug: str) -> Permission | None:
        """Any permission row (inactive / soft-deleted included)."""
        stmt = select(Permission).where(Permission.slug == slug)
        return (await self._session.scalars(stmt)).one_or_none()

    async def role_permission_slugs(
        self, role_id: UUID, *, live_only: bool = False
    ) -> frozenset[str]:
        """Permissions linked to the role; ``live_only`` drops inactive/deleted ones."""
        stmt = (
            select(Permission.slug)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role_id)
        )
        if live_only:
            stmt = stmt.where(Permission.is_active.is_(True), Permission.is_deleted.is_(False))
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
        return (await self._session.scalars(_locked(stmt))).one_or_none()

    async def upsert_override(
        self,
        *,
        user_id: UUID,
        permission_id: UUID,
        tenant_id: UUID | None,
        is_granted: bool,
        expires_at: datetime | None,
        reason: str | None,
        granted_by_id: UUID | None,
    ) -> tuple[UserPermissionOverride, bool]:
        """Upserted row and ``inserted`` (``False`` -> an existing row was updated)."""
        stmt = build_override_upsert(
            user_id=user_id,
            permission_id=permission_id,
            tenant_id=tenant_id,
            is_granted=is_granted,
            expires_at=expires_at,
            reason=reason,
            granted_by_id=granted_by_id,
        )
        result = await self._session.execute(stmt, execution_options={"populate_existing": True})
        row, inserted = result.one()
        return row, bool(inserted)

    async def role_users(self, role_id: UUID) -> list[User]:
        """Live users holding the role, locked ``FOR UPDATE`` (stable order)."""
        stmt = (
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .where(UserRole.role_id == role_id, User.is_deleted.is_(False))
            .order_by(User.id)
            .with_for_update(of=User)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(stmt)).all())

    async def tenant_moderators_of(self, user_id: UUID) -> list[TenantModerator]:
        """Every shop assignment of the user, locked ``FOR UPDATE``."""
        stmt = (
            select(TenantModerator)
            .where(TenantModerator.user_id == user_id)
            .order_by(TenantModerator.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(stmt)).all())

    async def holds_global_view_all(self, user_id: UUID, *, now: datetime) -> bool:
        """The user holds ``tenants.view_all`` globally (live role or global allow)."""
        from app.modules.iam.permissions import TENANTS_VIEW_ALL

        return (TENANTS_VIEW_ALL, None) in await self.user_granted_permissions(user_id, now=now)

    async def get_tenant_moderator(self, user_id: UUID, tenant_id: UUID) -> TenantModerator | None:
        stmt = select(TenantModerator).where(
            TenantModerator.user_id == user_id, TenantModerator.tenant_id == tenant_id
        )
        return (await self._session.scalars(stmt)).one_or_none()
