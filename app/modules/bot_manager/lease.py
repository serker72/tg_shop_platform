"""Renewable Redis ownership for one polling bot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from socket import gethostname
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from uuid import UUID

    from redis.asyncio import Redis

# Keep the public value hostname:pid. The private generation prevents ABA even
# for two workers in one process or a process restarted with the same PID.
_ACQUIRE = """
if not redis.call('set', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[3]) then return 0 end
redis.call('set', KEYS[2], ARGV[2], 'PX', ARGV[3])
return 1
"""
_MATCH = """
if redis.call('get', KEYS[1]) ~= ARGV[1] or redis.call('get', KEYS[2]) ~= ARGV[2]
then return 0 end
"""
_RENEW = (
    _MATCH
    + """
redis.call('pexpire', KEYS[1], ARGV[3])
redis.call('pexpire', KEYS[2], ARGV[3])
return 1
"""
)
_RELEASE = _MATCH + "return redis.call('del', KEYS[1], KEYS[2])"


_SET_MARK = (
    _MATCH
    + """
redis.call('set', KEYS[3], ARGV[3], 'EX', ARGV[4])
if #KEYS == 4 then return 1 + redis.call('del', KEYS[4]) end
return 1
"""
)


_TAKE_MARK = (
    _MATCH
    + """
if redis.call('set', KEYS[3], ARGV[3], 'NX', 'EX', ARGV[4]) then return 1 end
return 2
"""
)


def worker_identity() -> str:
    return f"{gethostname()}:{os.getpid()}"


@dataclass(frozen=True, slots=True)
class BotLease:
    """Single acquisition attempt; construct a fresh instance for each acquisition."""

    redis: Redis = field(repr=False)
    bot_id: UUID
    owner: str
    ttl_seconds: int
    _generation: str = field(default_factory=lambda: uuid4().hex, init=False, repr=False)

    @property
    def key(self) -> str:
        return f"bot:lease:{self.bot_id}"

    async def _eval(self, script: str) -> bool:
        return bool(
            await self.redis.eval(
                script,
                2,
                self.key,
                f"{self.key}:generation",
                self.owner,
                self._generation,
                self.ttl_seconds * 1000,
            )
        )

    async def set_mark(self, key: str, value: str, *, ttl: int, clear: str | None = None) -> int:
        """0: stale lease; 1: set; 2: set and removed the optional key."""
        keys: tuple[str, ...] = (self.key, f"{self.key}:generation", key)
        if clear is not None:
            keys += (clear,)
        return int(
            await self.redis.eval(
                _SET_MARK, len(keys), *keys, self.owner, self._generation, value, ttl
            )
        )

    async def take_mark(self, key: str, token: str, *, ttl: int) -> int:
        """0: stale lease; 1: acquired; 2: key already taken."""
        return int(
            await self.redis.eval(
                _TAKE_MARK,
                3,
                self.key,
                f"{self.key}:generation",
                key,
                self.owner,
                self._generation,
                token,
                ttl,
            )
        )

    async def acquire(self) -> bool:
        return await self._eval(_ACQUIRE)

    async def is_owner(self) -> bool:
        return await self._eval(_MATCH + "return 1")

    async def renew(self) -> bool:
        return await self._eval(_RENEW)

    async def release(self) -> bool:
        return await self._eval(_RELEASE)
