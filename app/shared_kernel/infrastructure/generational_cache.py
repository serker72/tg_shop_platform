"""Generation-counter Redis cache primitives shared by every cache-aside module.

* :data:`REDIS_ERRORS` - errors meaning "Redis is unavailable" (connection, timeout, OOM).
* :class:`RedisCircuitBreaker` - after ``threshold`` consecutive errors Redis is skipped for
  ``cooldown`` seconds; one instance per cache, log events are prefixed with ``name``.
* :class:`GenerationalCache` - failure-tolerant ``MGET``/guarded ``SET``/``INCR``+``DEL``
  over ``redis.asyncio.Redis``: a value is written only if every guard (generation key)
  still holds the expected value - one Lua script, so it cannot interleave with an ``INCR``.

Every Redis error is swallowed (``None``/``False``/``ok=False``), the caller falls back to
its source of truth. Key layout and value serialization belong to the caller.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

__all__ = [
    "REDIS_ERRORS",
    "GenerationalCache",
    "InvalidationResult",
    "RedisCircuitBreaker",
    "as_int",
]

#: Errors of a Redis call that mean "Redis is unavailable".
REDIS_ERRORS: Final = (RedisError, OSError, TimeoutError)

# KEYS[1] value key, KEYS[2..n] guard keys; ARGV[1] value, ARGV[2] ttl,
# ARGV[3..n+1] expected guard values (a missing guard key reads as '0').
_SET_IF_UNCHANGED: Final = """
for i = 2, #KEYS do
  local current = redis.call('GET', KEYS[i]) or '0'
  if current ~= ARGV[i + 1] then return 0 end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class InvalidationResult:
    """Outcome of an invalidation: ``ok=False`` means Redis did not confirm it."""

    removed: int = 0
    ok: bool = True


def as_int(value: bytes | str | None) -> int:
    """Value of a generation key: missing = 0; ``ValueError`` for a non-integer value."""
    return 0 if value is None else int(value)


class RedisCircuitBreaker:
    """Skip Redis for ``cooldown`` seconds after ``threshold`` consecutive errors."""

    def __init__(
        self,
        name: str,
        *,
        threshold: int = 3,
        cooldown: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._name = name
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock
        self._failures = 0
        self._cooling_until = 0.0

    @property
    def available(self) -> bool:
        """``False`` while the breaker is open (Redis recently failed)."""
        return self._clock() >= self._cooling_until

    def on_success(self) -> None:
        if self._cooling_until > 0.0 and self._failures >= self._threshold:
            _log.warning(f"{self._name}_recovered", failures=self._failures)
        self._failures = 0
        self._cooling_until = 0.0

    def on_error(self, event: str, error: BaseException, **ctx: Any) -> None:
        """Count a failure; ``event`` is logged as ``f"{name}_{event}"``."""
        self._failures += 1
        # Details below the threshold: a dead Redis must not flood the log with warnings.
        level = _log.warning if self._failures >= self._threshold else _log.debug
        level(
            f"{self._name}_{event}",
            **ctx,
            error=type(error).__name__,
            failures=self._failures,
        )
        # Re-arm on every failure past the threshold: after a cooldown that ended with one or
        # more probe failures the breaker closes again (``on_success`` clears the cooldown).
        if self._failures >= self._threshold:
            self._cooling_until = self._clock() + self._cooldown
            _log.warning(
                f"{self._name}_circuit_open",
                failures=self._failures,
                cooldown_seconds=self._cooldown,
            )


class GenerationalCache:
    """Failure-tolerant generation-guarded commands over ``redis.asyncio.Redis``.

    ``event``/``**ctx`` keyword arguments only shape the breaker log record on an error.
    """

    def __init__(self, redis: Redis, *, breaker: RedisCircuitBreaker) -> None:
        self._redis = redis
        self._breaker = breaker

    @property
    def available(self) -> bool:
        return self._breaker.available

    async def mget(
        self, *keys: str, event: str = "get_failed", **ctx: Any
    ) -> list[bytes | None] | None:
        """Raw values of ``keys``; ``None`` when Redis is unavailable (or the breaker is open)."""
        if not self._breaker.available:
            return None
        try:
            values = await self._redis.mget(*keys)
        except REDIS_ERRORS as error:
            self._breaker.on_error(event, error, **ctx)
            return None
        self._breaker.on_success()
        return [value.encode() if isinstance(value, str) else value for value in values]

    async def read_generations(
        self, *gen_keys: str, event: str = "generations_failed", **ctx: Any
    ) -> tuple[int, ...] | None:
        """Current values of the generation keys (missing = 0); ``None`` when unavailable."""
        values = await self.mget(*gen_keys, event=event, **ctx)
        return None if values is None else tuple(as_int(value) for value in values)

    async def set_if_unchanged(
        self,
        key: str,
        value: bytes,
        ttl: int,
        guards: Mapping[str, int],
        *,
        event: str = "set_failed",
        **ctx: Any,
    ) -> bool:
        """``SET key value EX ttl`` only if every guard key still holds its expected value.

        Guards are checked in the mapping's iteration (insertion) order. ``False`` - a guard
        changed meanwhile or Redis failed; nothing is written then.
        """
        if not self._breaker.available:
            return False
        guard_keys = list(guards)
        try:
            written = await self._redis.eval(
                _SET_IF_UNCHANGED,
                1 + len(guard_keys),
                key,
                *guard_keys,
                value,
                max(1, ttl),
                *(str(guards[guard]) for guard in guard_keys),
            )
        except REDIS_ERRORS as error:
            self._breaker.on_error(event, error, **ctx)
            return False
        self._breaker.on_success()
        return bool(written)

    async def bump(
        self,
        gen_key: str,
        *,
        delete: Sequence[str] = (),
        event: str = "invalidate_failed",
        **ctx: Any,
    ) -> InvalidationResult:
        """``INCR gen_key`` first, then ``DEL`` of ``delete`` (memory only).

        The bump is what invalidates; if ``DEL`` fails the call still reports ``ok=False`` -
        retry the whole call (a second ``INCR`` is harmless, the counter is monotonic).
        """
        if not self._breaker.available:
            return InvalidationResult(ok=False)
        removed = 0
        try:
            increment: Awaitable[int] = self._redis.incr(gen_key)
            await increment
            if delete:
                removed = int(await self._redis.delete(*delete))
        except REDIS_ERRORS as error:
            self._breaker.on_error(event, error, **ctx)
            return InvalidationResult(ok=False)
        self._breaker.on_success()
        return InvalidationResult(removed=removed, ok=True)

    async def delete(self, *keys: str, event: str = "delete_failed", **ctx: Any) -> bool | None:
        """``DEL keys``; ``True`` when done, ``None`` when Redis is unavailable."""
        if not self._breaker.available:
            return None
        try:
            await self._redis.delete(*keys)
        except REDIS_ERRORS as error:
            self._breaker.on_error(event, error, **ctx)
            return None
        self._breaker.on_success()
        return True
