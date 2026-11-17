"""Dishka provider of the system module.

Requires ``async_sessionmaker[AsyncSession]`` / ``AsyncSession`` (``DatabaseProvider``) and
``PermissionService`` (``IamProvider``).
"""

from __future__ import annotations

from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.iam.service import PermissionService
from app.modules.system.maintenance import MaintenanceGate
from app.modules.system.service import SystemSettingsService

__all__ = ["SystemProvider"]


class SystemProvider(Provider):
    """``MaintenanceGate`` (APP, one per process) and ``SystemSettingsService`` (REQUEST)."""

    @provide(scope=Scope.APP)
    def provide_maintenance_gate(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> MaintenanceGate:
        return MaintenanceGate(session_factory)

    @provide(scope=Scope.REQUEST)
    def provide_system_settings_service(
        self, session: AsyncSession, permissions: PermissionService, gate: MaintenanceGate
    ) -> SystemSettingsService:
        return SystemSettingsService(session, permissions, gate=gate)
