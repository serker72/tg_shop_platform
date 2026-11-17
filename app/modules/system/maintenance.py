"""``MaintenanceGate``: ``system_settings.is_maintenance_mode`` flag (decision Q4).

Reads the flag through :class:`~app.modules.system.cache.SystemSettingsCache` (Redis
snapshot + generation, no process-local cache): a change committed by
``SystemSettingsService`` is visible to every process on the next update. Redis down ->
PostgreSQL; PostgreSQL down -> last known value; before the first successful read the flag
is off (fail-open). Reused by the shop bots (task 12).
"""

from __future__ import annotations

from app.modules.system.cache import SystemSettingsCache

__all__ = ["MaintenanceGate"]


class MaintenanceGate:
    """Read-only view of the maintenance flag."""

    def __init__(self, settings: SystemSettingsCache) -> None:
        self._settings = settings

    async def is_on(self) -> bool:
        return (await self._settings.get()).is_maintenance_mode
