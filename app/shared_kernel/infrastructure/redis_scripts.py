"""Small shared Redis Lua scripts."""

from __future__ import annotations

from typing import Any, Final

__all__ = ["COMPARE_AND_DELETE_LUA", "compare_and_delete"]

#: Delete ``KEYS[1]`` only if it still holds ``ARGV[1]`` (owner token); returns 1/0.
COMPARE_AND_DELETE_LUA: Final = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"
)


async def compare_and_delete(redis: Any, key: str, token: str | bytes) -> bool:
    """Drop ``key`` while it still holds ``token`` - a key re-taken by someone else after
    expiry is never removed. Redis errors propagate (the caller decides how to log them)."""
    return bool(await redis.eval(COMPARE_AND_DELETE_LUA, 1, key, token))
