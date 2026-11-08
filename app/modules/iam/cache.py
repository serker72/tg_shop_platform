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
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.modules.iam.snapshot import PermissionSnapshot

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

# KEYS[1] snapshot key, KEYS[2] per-user generation, KEYS[3] global generation.
# ARGV[1] value, ARGV[2] ttl, ARGV[3] expected user gen, ARGV[4] expected global gen.
_SET_IF_UNCHANGED = """
local user_gen = redis.call('GET', KEYS[2]) or '0'
local global_gen = redis.call('GET', KEYS[3]) or '0'
if user_gen ~= ARGV[3] or global_gen ~= ARGV[4] then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""

_ERRORS = (RedisError, OSError, TimeoutError)
_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class InvalidationResult:
    """Outcome of an invalidation: ``ok=False`` means Redis did not confirm it."""

    removed: int = 0
    ok: bool = True


def _as_int(value: bytes | str | None) -> int:
    return 0 if value is None else int(value)


class PermissionCache:
    """Generation-aware, failure-tolerant wrapper over ``redis.asyncio.Redis``."""

    def __init__(self, redis: Redis, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._redis = redis
        self._clock = clock
        self._failures = 0
        self._cooling_until = 0.0

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
        return self._clock() >= self._cooling_until

    # --- reads ------------------------------------------------------------------------

    async def get_with_generations(
        self, telegram_user_id: int
    ) -> tuple[PermissionSnapshot, int, int] | None:
        """Snapshot plus the ``(user_gen, global_gen)`` stamps stored with it; ``None`` on miss."""
        if not self.available:
            return None
        try:
            raw, global_gen = await self._redis.mget(
                self.key(telegram_user_id), GLOBAL_GENERATION_KEY
            )
        except _ERRORS as error:
            self._on_error("permission_cache_get_failed", error, telegram_user_id)
            return None
        self._on_success()
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
        if not self.available:
            return None
        try:
            user_gen, global_gen = await self._redis.mget(
                self.user_generation_key(telegram_user_id), GLOBAL_GENERATION_KEY
            )
        except _ERRORS as error:
            self._on_error("permission_cache_generations_failed", error, telegram_user_id)
            return None
        self._on_success()
        return _as_int(user_gen), _as_int(global_gen)

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
        try:
            written = await self._redis.eval(
                _SET_IF_UNCHANGED,
                3,
                self.key(telegram_user_id),
                self.user_generation_key(telegram_user_id),
                GLOBAL_GENERATION_KEY,
                stamped.to_json(),
                max(1, ttl_seconds),
                str(generations[0]),
                str(generations[1]),
            )
        except _ERRORS as error:
            self._on_error("permission_cache_set_failed", error, telegram_user_id)
            return False
        self._on_success()
        if not written:
            _log.debug("permission_cache_write_skipped", key=self.key(telegram_user_id))
        return bool(written)

    async def delete(self, telegram_user_id: int) -> bool | None:
        """``DEL`` the snapshot only; ``None`` when Redis is unavailable."""
        if not self.available:
            return None
        try:
            await self._redis.delete(self.key(telegram_user_id))
        except _ERRORS as error:
            self._on_error("permission_cache_delete_failed", error, telegram_user_id)
            return None
        self._on_success()
        return True

    # --- invalidation -----------------------------------------------------------------

    async def invalidate(self, telegram_user_id: int) -> InvalidationResult:
        """``INCR user:{tg}:perm_gen`` first, then ``DEL`` the snapshot.

        The generation bump is what invalidates (the snapshot stops matching); ``DEL`` only
        frees memory. If ``DEL`` fails the snapshot is already dead, yet the call reports
        ``ok=False``: the caller must retry the *whole* invalidation (a second ``INCR`` is
        harmless - the generation is a monotonic counter).
        """
        if not self.available:
            return InvalidationResult(ok=False)
        try:
            await self._incr(self.user_generation_key(telegram_user_id))
            removed = await self._redis.delete(self.key(telegram_user_id))
        except _ERRORS as error:
            self._on_error("permission_cache_invalidate_failed", error, telegram_user_id)
            return InvalidationResult(ok=False)
        self._on_success()
        return InvalidationResult(removed=int(removed), ok=True)

    async def invalidate_all(self) -> InvalidationResult:
        """``INCR perms:gen`` - every snapshot stops matching; ``ok=False`` on a Redis error.

        No ``SCAN``: contrary to ``KEYS``/``SCAN`` this is a single O(1) command, so it stays
        correct on a Redis Cluster as long as the permission keys live in one hash slot
        (they do not share a tag today - relevant only if the deployment ever becomes a
        cluster; a key-hash tag would be needed there).
        """
        if not self.available:
            return InvalidationResult(ok=False)
        try:
            await self._incr(GLOBAL_GENERATION_KEY)
        except _ERRORS as error:
            self._on_error("permission_cache_invalidate_all_failed", error, None)
            return InvalidationResult(ok=False)
        self._on_success()
        return InvalidationResult(ok=True)

    # --- commands ---------------------------------------------------------------------

    async def _incr(self, key: str) -> int:
        """``INCR key`` returning the new value (explicit coroutine - no implicit-awaits)."""
        increment: Awaitable[int] = self._redis.incr(key)
        return int(await increment)

    # --- circuit breaker --------------------------------------------------------------

    def _on_success(self) -> None:
        if self._cooling_until > 0.0 and self._failures >= _FAILURE_THRESHOLD:
            _log.warning("permission_cache_recovered", failures=self._failures)
        self._failures = 0
        self._cooling_until = 0.0

    def _on_error(self, event: str, error: Exception, telegram_user_id: int | None) -> None:
        self._failures += 1
        # Details below the threshold: a dead Redis must not flood the log with warnings.
        level = _log.warning if self._failures >= _FAILURE_THRESHOLD else _log.debug
        level(
            event,
            telegram_user_id=telegram_user_id,
            error=type(error).__name__,
            failures=self._failures,
        )
        # Re-arm on every failure past the threshold: after a cooldown that ended with one or
        # more probe failures the breaker closes again (``_on_success`` clears the cooldown).
        if self._failures >= _FAILURE_THRESHOLD:
            self._cooling_until = self._clock() + _COOLDOWN_SECONDS
            _log.warning(
                "permission_cache_circuit_open",
                failures=self._failures,
                cooldown_seconds=_COOLDOWN_SECONDS,
            )
