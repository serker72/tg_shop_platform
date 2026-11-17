"""Dishka provider of the IAM module.

Requires ``redis.asyncio.Redis`` (``RedisProvider``), ``AsyncSession``
(``DatabaseProvider``) and ``SystemSettingsCache`` (``SystemProvider``).
"""

from __future__ import annotations

from dishka import Provider, Scope, provide
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam.admin_service import (
    AccessAdminService,
    RoleAdminService,
    TenantAdminService,
    UserAdminService,
)
from app.modules.iam.cache import PermissionCache
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.iam.service import PermissionService
from app.modules.system.cache import SystemSettingsCache

__all__ = ["IamProvider"]


class IamProvider(Provider):
    """``PermissionCache`` (APP), ``PermissionService`` and admin services (REQUEST)."""

    @provide(scope=Scope.APP)
    def provide_permission_cache(self, redis: Redis) -> PermissionCache:
        """Process-wide cache over the shared Redis client."""
        return PermissionCache(redis)

    @provide(scope=Scope.REQUEST)
    def provide_permission_service(
        self, session: AsyncSession, cache: PermissionCache, settings_cache: SystemSettingsCache
    ) -> PermissionService:
        """Permission checker bound to the request session (TTL from the settings cache)."""
        return PermissionService(session, cache, settings_cache=settings_cache)

    @provide(scope=Scope.REQUEST)
    def provide_tenant_admin_service(
        self, session: AsyncSession, permissions: PermissionService
    ) -> TenantAdminService:
        return TenantAdminService(session, permissions)

    @provide(scope=Scope.REQUEST)
    def provide_user_admin_service(
        self, session: AsyncSession, permissions: PermissionService
    ) -> UserAdminService:
        return UserAdminService(session, permissions)

    @provide(scope=Scope.REQUEST)
    def provide_role_admin_service(
        self, session: AsyncSession, permissions: PermissionService
    ) -> RoleAdminService:
        return RoleAdminService(session, permissions)

    @provide(scope=Scope.REQUEST)
    def provide_access_admin_service(
        self, session: AsyncSession, permissions: PermissionService
    ) -> AccessAdminService:
        return AccessAdminService(session, permissions)

    @provide(scope=Scope.REQUEST)
    def provide_admin_queries(self, session: AsyncSession) -> IamAdminQueries:
        """Read-only queries of the Master Bot IAM dialogs."""
        return IamAdminQueries(session)
