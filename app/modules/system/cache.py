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

Reserve snapshot: every successful read (Redis hit or PostgreSQL) replaces it and restarts
a :data:`PG_FALLBACK_SECONDS` window (monotonic clock); a failed PostgreSQL attempt restarts
the window too. Redis unavailable (own circuit breaker ``settings_cache`` open, or a Redis
error): inside the window the reserve snapshot is returned (the defaults if there is none)
without touching PostgreSQL, outside it PostgreSQL is read. PostgreSQL error -> the reserve
snapshot; before the first successful read -> :data:`DEFAULT_SETTINGS` (maintenance off,
default IAM cache TTL).

A missing row reads as the defaults and :data:`DEFAULT_SETTINGS` is written to Redis by the
same guarded write (no PostgreSQL read per call). A non-integer ``settings:gen`` (corrupted
key) is deleted by ``compare_and_delete`` (only while it still holds the same bad value),
logged as error ``settings_cache_generation_invalid``, and the read continues with
generation 0 (the guarded write treats the missing key as ``'0'``). The value changed
meanwhile -> a miss without write; a Redis error while deleting -> as Redis unavailable.
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
    REDIS_ERRORS,
    GenerationalCache,
    InvalidationResult,
    RedisCircuitBreaker,
    as_int,
)
from app.shared_kernel.infrastructure.redis_scripts import compare_and_delete

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
        self._breaker = RedisCircuitBreaker(_BREAKER_NAME, clock=clock)
        self._cache = GenerationalCache(redis, breaker=self._breaker)
        self._session_factory = session_factory
        self._ttl = ttl_seconds
        self._fallback = fallback_seconds
        self._clock = clock
        self._redis = redis
        #: Reserve snapshot: the last one seen from Redis or PostgreSQL.
        self._last: SettingsSnapshot | None = None
        #: Monotonic time of the last successful read (Redis or PostgreSQL) or of the last
        #: PostgreSQL attempt (failed ones too): start of the Redis-down reuse window.
        self._checked_at: float | None = None

    async def get(self) -> SettingsSnapshot:
        """Current settings; never raises (see the module docstring for the fallbacks)."""
        values = await self._cache.mget(SNAPSHOT_KEY, GENERATION_KEY, event="get_failed")
        if values is None:
            return await self._get_without_redis()
        raw, raw_generation = values
        generation = _parse_generation(raw_generation)
        if generation is None and raw_generation is not None:
            repaired = await self._repair_generation(raw_generation)
            if repaired is None:  # Redis failed while repairing: treat as unavailable
                return await self._get_without_redis()
            generation = 0 if repaired else None
            raw = None  # read next to a corrupted generation: never trusted
        cached = None if raw is None else SettingsSnapshot.from_json(raw)
        if cached is not None and generation is not None and cached[1] == generation:
            self._remember(cached[0])
            return cached[0]
        snapshot = await self._load()
        if snapshot is None:
            return self._last or DEFAULT_SETTINGS
        if generation is not None:
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

    def _remember(self, snapshot: SettingsSnapshot) -> None:
        self._last = snapshot
        self._checked_at = self._clock()

    async def _repair_generation(self, raw: bytes) -> bool | None:
        """Delete a corrupted ``settings:gen`` only if it still holds ``raw``.

        ``True`` - deleted (continue with generation 0); ``False`` - changed meanwhile (miss
        without write); ``None`` - Redis error (counted by the breaker).
        """
        try:
            deleted = await compare_and_delete(self._redis, GENERATION_KEY, raw)
        except REDIS_ERRORS as error:
            self._breaker.on_error("repair_failed", error)
            return None
        self._breaker.on_success()
        _log.error(
            "settings_cache_generation_invalid",
            key=GENERATION_KEY,
            value=raw[:32].decode(errors="replace"),
            deleted=deleted,
        )
        return deleted

    async def _get_without_redis(self) -> SettingsSnapshot:
        """Redis unavailable: within the window - the reserve snapshot (or the defaults)
        without touching PostgreSQL, else PostgreSQL."""
        if self._checked_at is not None and self._clock() - self._checked_at < self._fallback:
            return self._last or DEFAULT_SETTINGS
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
            # A failed attempt also opens the window: no PostgreSQL timeout on every call.
            self._checked_at = self._clock()
            if self._last is not None:
                _log.warning("system_settings_read_failed", error=type(error).__name__)
            else:
                _log.error("system_settings_read_failed_fail_open", error=type(error).__name__)
            return None
        self._remember(snapshot)
        return snapshot


def _parse_generation(raw: bytes | None) -> int | None:
    """``settings:gen`` value (missing = 0); ``None`` for a corrupted (non-integer) value."""
    try:
        return as_int(raw)
    except ValueError:
        return None
