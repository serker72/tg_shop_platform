"""Redis cache-aside storage of :class:`PermissionSnapshot` with generation counters.

Keys (all in the configured ``REDIS_DB``)::

    perms:gen                    global generation  (INCR = drop every snapshot)
    user:{tg}:permissions        snapshot JSON      (SET ... EX ttl, carries both generations)
    user:{tg}:perm_gen           per-user generation (INCR = drop this user's snapshot)

How stale writes are prevented (risk 1A): a reader records ``(user_gen, global_gen)``
before loading from PostgreSQL and then runs one Lua script that writes the snapshot only
if both keys still hold those values - a check-and-set that cannot interleave with an
``INCR``. ``invalidate(tg)`` bumps ``user:{tg}:perm_gen`` (and deletes the snapshot),
``invalidate_all()`` bumps ``perms:gen``; no SCAN is needed any more.

Read path: one ``MGET`` of the snapshot and ``perms:gen``; the snapshot is accepted only if
its stamped global generation equals the current one. The per-user generation needs no
read: the snapshot key itself embeds the user, and a bump *deletes* it, so a surviving key
is by construction newer than any earlier bump (a snapshot written after the bump carries
the new user generation through the guarded write). A value stamped with an unknown/older
global generation - or undecodable - is a miss.

The generation keys are tiny and never expire (a per-user key survives the user's snapshot
TTL): they accumulate one short key per user ever invalidated plus one global key. That is
the price of a technically sound invalidation; a periodic cleanup is out of scope.

Redis is optional: every Redis/network error is swallowed (logged by the caller's service),
the request then reads PostgreSQL. The cache object carries a small circuit breaker so a
dead Redis costs one timeout per ``_FAILURE_THRESHOLD`` checks instead of one per call.

The mechanics (breaker, guarded write, bump) live in
:mod:`app.shared_kernel.infrastructure.generational_cache`; this class only owns the key
layout and the :class:`PermissionSnapshot` serialization.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import structlog
from redis.asyncio import Redis

from app.modules.iam.snapshot import PermissionSnapshot
from app.shared_kernel.infrastructure.generational_cache import (
    GenerationalCache,
    InvalidationResult,
    RedisCircuitBreaker,
)

__all__ = [
    "GLOBAL_GENERATION_KEY",
    "PERMISSION_KEY_PATTERN",
    "InvalidationResult",
    "PermissionCache",
]

#: ``SCAN MATCH`` pattern of every permission snapshot key (diagnostics / cleanup only).
PERMISSION_KEY_PATTERN = "user:*:permissions"
#: Generation of the whole permission state in this Redis database.
GLOBAL_GENERATION_KEY = "perms:gen"
_USER_GENERATION_KEY = "user:{telegram_user_id}:perm_gen"

#: Consecutive errors before Redis is skipped; and the pause length in seconds.
_FAILURE_THRESHOLD = 3
_COOLDOWN_SECONDS = 5.0
#: Prefix of the breaker log events (``permission_cache_get_failed`` etc.).
_BREAKER_NAME = "permission_cache"

_log = structlog.get_logger(__name__)


def _as_int(value: bytes | str | None) -> int:
    return 0 if value is None else int(value)


class PermissionCache:
    """Generation-aware, failure-tolerant wrapper over ``redis.asyncio.Redis``."""

    def __init__(self, redis: Redis, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._cache = GenerationalCache(
            redis,
            breaker=RedisCircuitBreaker(
                _BREAKER_NAME,
                threshold=_FAILURE_THRESHOLD,
                cooldown=_COOLDOWN_SECONDS,
                clock=clock,
            ),
        )

    # --- keys -------------------------------------------------------------------------

    @staticmethod
    def key(telegram_user_id: int) -> str:
        """Snapshot key ``user:{telegram_user_id}:permissions``."""
        return f"user:{telegram_user_id}:permissions"

    @staticmethod
    def user_generation_key(telegram_user_id: int) -> str:
        """Per-user generation key ``user:{telegram_user_id}:perm_gen``."""
        return _USER_GENERATION_KEY.format(telegram_user_id=telegram_user_id)

    @property
    def available(self) -> bool:
        """``False`` while the breaker is open (Redis recently failed)."""
        return self._cache.available

    # --- reads ------------------------------------------------------------------------

    async def get_with_generations(
        self, telegram_user_id: int
    ) -> tuple[PermissionSnapshot, int, int] | None:
        """Snapshot plus the ``(user_gen, global_gen)`` stamps stored with it; ``None`` on miss."""
        values = await self._cache.mget(
            self.key(telegram_user_id),
            GLOBAL_GENERATION_KEY,
            event="get_failed",
            telegram_user_id=telegram_user_id,
        )
        if values is None:
            return None
        raw, global_gen = values
        if raw is None:
            return None
        snapshot = PermissionSnapshot.from_json(raw)
        if snapshot is None or snapshot.global_generation != _as_int(global_gen):
            _log.debug("permission_cache_stale_or_invalid", key=self.key(telegram_user_id))
            return None
        return snapshot, snapshot.generation, snapshot.global_generation

    async def get(self, telegram_user_id: int) -> PermissionSnapshot | None:
        """Cached snapshot valid for the current generations; ``None`` otherwise."""
        loaded = await self.get_with_generations(telegram_user_id)
        return None if loaded is None else loaded[0]

    async def read_generations(self, telegram_user_id: int) -> tuple[int, int] | None:
        """Current ``(user_gen, global_gen)``; ``None`` when Redis is unavailable."""
        generations = await self._cache.read_generations(
            self.user_generation_key(telegram_user_id),
            GLOBAL_GENERATION_KEY,
            event="generations_failed",
            telegram_user_id=telegram_user_id,
        )
        if generations is None:
            return None
        user_gen, global_gen = generations
        return user_gen, global_gen

    # --- writes -----------------------------------------------------------------------

    async def set(
        self,
        snapshot: PermissionSnapshot,
        telegram_user_id: int,
        ttl_seconds: int,
        *,
        generations: tuple[int, int],
    ) -> bool:
        """Write ``snapshot`` only if both generations still equal ``generations``.

        Returns ``False`` when the generations changed (someone invalidated meanwhile) or
        Redis failed - nothing is written then.
        """
        if not self.available:
            return False
        stamped = snapshot.with_generations(
            user_generation=generations[0], global_generation=generations[1]
        )
        written = await self._cache.set_if_unchanged(
            self.key(telegram_user_id),
            stamped.to_json(),
            ttl_seconds,
            {
                self.user_generation_key(telegram_user_id): generations[0],
                GLOBAL_GENERATION_KEY: generations[1],
            },
            event="set_failed",
            telegram_user_id=telegram_user_id,
        )
        if not written:
            _log.debug("permission_cache_write_skipped", key=self.key(telegram_user_id))
        return bool(written)

    async def delete(self, telegram_user_id: int) -> bool | None:
        """``DEL`` the snapshot only; ``None`` when Redis is unavailable."""
        return await self._cache.delete(
            self.key(telegram_user_id), event="delete_failed", telegram_user_id=telegram_user_id
        )

    # --- invalidation -----------------------------------------------------------------

    async def invalidate(self, telegram_user_id: int) -> InvalidationResult:
        """``INCR user:{tg}:perm_gen`` first, then ``DEL`` the snapshot.

        The generation bump is what invalidates (the snapshot stops matching); ``DEL`` only
        frees memory. If ``DEL`` fails the snapshot is already dead, yet the call reports
        ``ok=False``: the caller must retry the *whole* invalidation (a second ``INCR`` is
        harmless - the generation is a monotonic counter).
        """
        return await self._cache.bump(
            self.user_generation_key(telegram_user_id),
            delete=(self.key(telegram_user_id),),
            event="invalidate_failed",
            telegram_user_id=telegram_user_id,
        )

    async def invalidate_all(self) -> InvalidationResult:
        """``INCR perms:gen`` - every snapshot stops matching; ``ok=False`` on a Redis error.

        No ``SCAN``: contrary to ``KEYS``/``SCAN`` this is a single O(1) command, so it stays
        correct on a Redis Cluster as long as the permission keys live in one hash slot
        (they do not share a tag today - relevant only if the deployment ever becomes a
        cluster; a key-hash tag would be needed there).
        """
        return await self._cache.bump(
            GLOBAL_GENERATION_KEY, event="invalidate_all_failed", telegram_user_id=None
        )
