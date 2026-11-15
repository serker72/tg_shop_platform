"""``MaintenanceGate``: cached ``system_settings.is_maintenance_mode`` flag (decision Q4).

The flag is read from PostgreSQL at most once per ``ttl`` seconds per process (a short TTL
keeps every update from hitting the DB); concurrent callers after expiry share one read
(``asyncio.Lock``). A missing singleton row means "off". A DB error keeps the last known
value (fail-open ``False`` before the first successful read) for one more TTL. Reused by
the shop bots (task 12).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.system.repository import SystemSettingsRepository
from app.shared_kernel.domain.exceptions import EntityNotFoundError

__all__ = ["DEFAULT_MAINTENANCE_TTL_SECONDS", "MaintenanceGate"]

DEFAULT_MAINTENANCE_TTL_SECONDS = 5.0

_log = structlog.get_logger(__name__)


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
        self._known = False
        self._expires_at = float("-inf")
        self._lock = asyncio.Lock()

    async def is_on(self) -> bool:
        if self._clock() < self._expires_at:
            return self._value
        async with self._lock:
            now = self._clock()
            if now < self._expires_at:  # refreshed by a concurrent caller
                return self._value
            self._value = await self._read()
            self._expires_at = now + self._ttl
            return self._value

    async def _read(self) -> bool:
        try:
            async with self._session_factory() as session:
                try:
                    settings = await SystemSettingsRepository(session).get()
                except EntityNotFoundError:
                    value = False
                else:
                    value = bool(settings.is_maintenance_mode)
        except SQLAlchemyError as error:
            if self._known:
                _log.warning(
                    "maintenance_flag_read_failed", error=type(error).__name__, kept=self._value
                )
                return self._value
            _log.error("maintenance_flag_read_failed_fail_open", error=type(error).__name__)
            return False
        self._known = True
        return value
