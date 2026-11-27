"""Contract of the Redis lease commands without a live Redis dependency."""

from typing import cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.modules.bot_manager.lease import BotLease


@pytest.mark.req("R-II.9")
async def test_same_owner_uses_distinct_generation_per_acquisition() -> None:
    redis = Mock(spec=Redis)
    redis.eval = AsyncMock(return_value=1)
    bot_id = uuid4()
    old = BotLease(cast(Redis, redis), bot_id, "hostname:7", 5)
    new = BotLease(cast(Redis, redis), bot_id, "hostname:7", 5)

    assert await old.acquire()
    assert await new.acquire()
    first, second = (call.args for call in redis.eval.await_args_list)
    acquire, numkeys, key, generation_key, owner, old_generation, ttl = first
    assert "'NX', 'PX'" in acquire
    assert (numkeys, key, generation_key, owner, ttl) == (
        2,
        f"bot:lease:{bot_id}",
        f"bot:lease:{bot_id}:generation",
        "hostname:7",
        5000,
    )
    assert second[-2] != old_generation

    redis.eval.return_value = 0
    assert not await old.renew()
    assert not await old.release()
    for call in redis.eval.await_args_list[-2:]:
        script, *args = call.args
        assert "KEYS[1]) ~= ARGV[1]" in script
        assert "KEYS[2]) ~= ARGV[2]" in script
        assert tuple(args) == first[1:]
