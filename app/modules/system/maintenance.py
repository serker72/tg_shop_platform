"""``MaintenanceGate``: cached ``system_settings.is_maintenance_mode`` flag (decision Q4).

The flag is read from PostgreSQL at most once per ``ttl`` seconds per process (a short TTL
keeps every update from hitting the DB); a missing singleton row means "off". Reused by the
shop bots (task 12).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.system.repository import SystemSettingsRepository
from app.shared_kernel.domain.exceptions import EntityNotFoundError

__all__ = ["DEFAULT_MAINTENANCE_TTL_SECONDS", "MaintenanceGate"]

DEFAULT_MAINTENANCE_TTL_SECONDS = 5.0


class MaintenanceGate:
    """Read-only, process-local cache of the maintenance flag."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ttl: float = DEFAULT_MAINTENANCE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._ttl = ttl
        self._clock = clock
        self._value = False
        self._expires_at = float("-inf")

    async def is_on(self) -> bool:
        now = self._clock()
        if now < self._expires_at:
            return self._value
        async with self._session_factory() as session:
            try:
                settings = await SystemSettingsRepository(session).get()
            except EntityNotFoundError:
                value = False
            else:
                value = bool(settings.is_maintenance_mode)
        self._value = value
        self._expires_at = now + self._ttl
        return value
