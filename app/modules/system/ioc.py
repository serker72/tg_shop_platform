"""Dishka provider of the system module.

Requires ``async_sessionmaker[AsyncSession]`` / ``AsyncSession`` (``DatabaseProvider``),
``redis.asyncio.Redis`` (``RedisProvider``) and ``PermissionService`` (``IamProvider``).
``IamProvider`` in turn needs :class:`SystemSettingsCache` from this provider.
"""

from __future__ import annotations

from dishka import Provider, Scope, provide
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.iam.service import PermissionService
from app.modules.system.cache import SystemSettingsCache
from app.modules.system.maintenance import MaintenanceGate
from app.modules.system.service import SystemSettingsService

__all__ = ["SystemProvider"]


class SystemProvider(Provider):
    """``SystemSettingsCache`` / ``MaintenanceGate`` (APP) and ``SystemSettingsService``."""

    @provide(scope=Scope.APP)
    def provide_settings_cache(
        self, redis: Redis, session_factory: async_sessionmaker[AsyncSession]
    ) -> SystemSettingsCache:
        return SystemSettingsCache(redis, session_factory)

    @provide(scope=Scope.APP)
    def provide_maintenance_gate(self, settings_cache: SystemSettingsCache) -> MaintenanceGate:
        return MaintenanceGate(settings_cache)

    @provide(scope=Scope.REQUEST)
    def provide_system_settings_service(
        self,
        session: AsyncSession,
        permissions: PermissionService,
        settings_cache: SystemSettingsCache,
    ) -> SystemSettingsService:
        return SystemSettingsService(session, permissions, settings_cache=settings_cache)
