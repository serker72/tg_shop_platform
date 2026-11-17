"""``SystemSettingsCache``: snapshot of the ``system_settings`` row in Redis (R-II.5, R7-3).

Keys (in the configured ``REDIS_DB``)::

    settings:gen        generation (INCR after every committed change)
    settings:snapshot   JSON of the whole row + ``gen`` (SET ... EX ttl, guarded write)

Read path: one ``MGET`` of both keys; the snapshot is used only if its ``gen`` equals the
current generation. Otherwise the row is read from PostgreSQL (short session of the factory,
never the request session) and written back by one Lua script that checks that
``settings:gen`` still holds the generation read BEFORE the PostgreSQL read - a change
committed meanwhile (``invalidate`` bumps the generation) can never be overwritten by a
stale row. The TTL is only a safety net.

No process-local cache while Redis answers: every call costs one ``MGET`` - a change is
visible to every process right after :meth:`SystemSettingsCache.invalidate`.

Redis unavailable (own circuit breaker ``settings_cache`` open, or a Redis error of the
``MGET``): the last snapshot read from PostgreSQL by this instance is reused while it is
younger than :data:`PG_FALLBACK_SECONDS` (monotonic clock), otherwise PostgreSQL is read
again. PostgreSQL error -> the last snapshot this instance has seen; before the first
successful read -> :data:`DEFAULT_SETTINGS` (maintenance off, default IAM cache TTL).

A missing row reads as the defaults and :data:`DEFAULT_SETTINGS` is written to Redis by the
same guarded write (no PostgreSQL read per call). A non-integer ``settings:gen`` (corrupted
key) is a miss that can never be cached: PostgreSQL is read on every call (logged as a
warning) until the key is repaired (``DEL settings:gen``); ``invalidate`` fails meanwhile
(``INCR`` of a non-integer).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Final

import structlog
from redis.asyncio import Redis
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.system.models import (
    DEFAULT_AUDIT_LOGS_RETENTION_MONTHS,
    DEFAULT_FILE_LOGS_RETENTION_DAYS,
    DEFAULT_IAM_CACHE_TTL_SECONDS,
    DEFAULT_USER_ACTIVITY_RETENTION_MONTHS,
    SystemSettings,
)
from app.modules.system.repository import SystemSettingsRepository
from app.shared_kernel.domain.exceptions import EntityNotFoundError
from app.shared_kernel.infrastructure.generational_cache import (
    GenerationalCache,
    InvalidationResult,
    RedisCircuitBreaker,
    as_int,
)

__all__ = [
    "DEFAULT_SETTINGS",
    "GENERATION_KEY",
    "PG_FALLBACK_SECONDS",
    "SETTINGS_KEY_PATTERN",
    "SNAPSHOT_KEY",
    "SNAPSHOT_TTL_SECONDS",
    "SettingsSnapshot",
    "SystemSettingsCache",
]

SNAPSHOT_KEY: Final = "settings:snapshot"
GENERATION_KEY: Final = "settings:gen"
#: ``SCAN MATCH`` pattern of every key of this cache (test cleanup).
SETTINGS_KEY_PATTERN: Final = "settings:*"
#: Safety-net TTL of the snapshot key; correctness comes from the generation.
SNAPSHOT_TTL_SECONDS: Final = 60
#: Redis unavailable: reuse the last PostgreSQL snapshot of this instance for this long.
PG_FALLBACK_SECONDS: Final = 5.0
_BREAKER_NAME: Final = "settings_cache"

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    """Immutable copy of the ``system_settings`` row (defaults = column defaults)."""

    is_maintenance_mode: bool = False
    iam_cache_ttl_seconds: int = DEFAULT_IAM_CACHE_TTL_SECONDS
    user_activity_retention_months: int = DEFAULT_USER_ACTIVITY_RETENTION_MONTHS
    audit_logs_retention_months: int = DEFAULT_AUDIT_LOGS_RETENTION_MONTHS
    file_logs_retention_days: int = DEFAULT_FILE_LOGS_RETENTION_DAYS
    extra_config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_model(cls, row: SystemSettings) -> SettingsSnapshot:
        return cls(
            is_maintenance_mode=bool(row.is_maintenance_mode),
            iam_cache_ttl_seconds=int(row.iam_cache_ttl_seconds),
            user_activity_retention_months=int(row.user_activity_retention_months),
            audit_logs_retention_months=int(row.audit_logs_retention_months),
            file_logs_retention_days=int(row.file_logs_retention_days),
            extra_config=dict(row.extra_config or {}),
        )

    def to_json(self, generation: int) -> bytes:
        payload = {**asdict(self), "gen": generation}
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()

    @classmethod
    def from_json(cls, raw: bytes | str) -> tuple[SettingsSnapshot, int] | None:
        """``(snapshot, gen)``; ``None`` for anything undecodable or mistyped."""
        try:
            data = json.loads(raw)
            generation = data.pop("gen")
            snapshot = cls(**data)
        except (ValueError, TypeError, KeyError, AttributeError):
            return None
        numbers = (
            generation,
            snapshot.iam_cache_ttl_seconds,
            snapshot.user_activity_retention_months,
            snapshot.audit_logs_retention_months,
            snapshot.file_logs_retention_days,
        )
        if (
            not isinstance(snapshot.is_maintenance_mode, bool)
            or not isinstance(snapshot.extra_config, dict)
            or any(isinstance(n, bool) or not isinstance(n, int) for n in numbers)
        ):
            return None
        return snapshot, generation


#: Used before the first successful read (fail-open: maintenance off).
DEFAULT_SETTINGS: Final = SettingsSnapshot()


class SystemSettingsCache:
    """Process-wide (APP scope) reader of the settings snapshot (see the module docstring)."""

    def __init__(
        self,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ttl_seconds: int = SNAPSHOT_TTL_SECONDS,
        fallback_seconds: float = PG_FALLBACK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cache = GenerationalCache(
            redis, breaker=RedisCircuitBreaker(_BREAKER_NAME, clock=clock)
        )
        self._session_factory = session_factory
        self._ttl = ttl_seconds
        self._fallback = fallback_seconds
        self._clock = clock
        #: Last snapshot seen from any source (PostgreSQL error fallback).
        self._last: SettingsSnapshot | None = None
        #: Last snapshot read from PostgreSQL and its monotonic read time (Redis-down reuse).
        self._pg: tuple[SettingsSnapshot, float] | None = None

    async def get(self) -> SettingsSnapshot:
        """Current settings; never raises (see the module docstring for the fallbacks)."""
        values = await self._cache.mget(SNAPSHOT_KEY, GENERATION_KEY, event="get_failed")
        if values is None:
            return await self._get_without_redis()
        raw, raw_generation = values
        generation = _parse_generation(raw_generation)
        cached = None if raw is None else SettingsSnapshot.from_json(raw)
        if cached is not None and generation is not None and cached[1] == generation:
            self._last = cached[0]
            return cached[0]
        snapshot = await self._load()
        if snapshot is None:
            return self._last or DEFAULT_SETTINGS
        if generation is None:
            _log.warning("settings_cache_generation_invalid", key=GENERATION_KEY)
        else:
            written = await self._cache.set_if_unchanged(
                SNAPSHOT_KEY,
                snapshot.to_json(generation),
                self._ttl,
                {GENERATION_KEY: generation},
                event="set_failed",
            )
            if not written:
                _log.debug("settings_cache_write_skipped", generation=generation)
        return snapshot

    async def invalidate(self) -> InvalidationResult:
        """``INCR settings:gen`` then ``DEL settings:snapshot``. Call AFTER the commit."""
        return await self._cache.bump(
            GENERATION_KEY, delete=(SNAPSHOT_KEY,), event="invalidate_failed"
        )

    async def _get_without_redis(self) -> SettingsSnapshot:
        """Redis unavailable: a fresh enough PostgreSQL snapshot, else PostgreSQL."""
        if self._pg is not None and self._clock() - self._pg[1] < self._fallback:
            return self._pg[0]
        snapshot = await self._load()
        return snapshot if snapshot is not None else self._last or DEFAULT_SETTINGS

    async def _load(self) -> SettingsSnapshot | None:
        """Row from PostgreSQL (missing row -> defaults); ``None`` on a database error.

        Connection errors may surface unwrapped (``OSError`` / ``TimeoutError``), not only
        as ``SQLAlchemyError``: all of them keep the "never raises" contract.
        """
        try:
            async with self._session_factory() as session:
                try:
                    row = await SystemSettingsRepository(session).get()
                except EntityNotFoundError:
                    snapshot = DEFAULT_SETTINGS
                else:
                    snapshot = SettingsSnapshot.from_model(row)
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            if self._last is not None:
                _log.warning("system_settings_read_failed", error=type(error).__name__)
            else:
                _log.error("system_settings_read_failed_fail_open", error=type(error).__name__)
            return None
        self._last = snapshot
        self._pg = (snapshot, self._clock())
        return snapshot


def _parse_generation(raw: bytes | None) -> int | None:
    """``settings:gen`` value (missing = 0); ``None`` for a corrupted (non-integer) value."""
    try:
        return as_int(raw)
    except ValueError:
        return None
