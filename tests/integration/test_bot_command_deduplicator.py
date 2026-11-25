"""Exercise the bot command deduplication scripts against real Redis."""

from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.entrypoints.bot_worker.commands import (
    COMPLETED_TTL_SECONDS,
    ClaimState,
    RedisCommandDeduplicator,
)

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.8")]


async def test_redis_command_claim_ownership_and_completed_redelivery(redis_client: Redis) -> None:
    ttl_seconds = 60
    dedupe = RedisCommandDeduplicator(redis_client, ttl_seconds=ttl_seconds)
    key = uuid4().hex
    lease_key, completed_key = f"bc:i:{key}", f"bc:c:{key}"
    try:
        owner = await dedupe.claim(key)
        assert isinstance(owner, str) and owner
        assert 0 < await redis_client.ttl(lease_key) <= ttl_seconds
        assert await dedupe.claim(key) is ClaimState.IN_FLIGHT

        await dedupe.release(key, owner)
        next_owner = await dedupe.claim(key)
        assert isinstance(next_owner, str) and next_owner and next_owner != owner

        assert await redis_client.expire(lease_key, 30)
        assert not await dedupe.renew(key, owner)
        assert 0 < await redis_client.ttl(lease_key) <= 30
        assert not await dedupe.complete(key, owner)
        await dedupe.release(key, owner)
        assert await dedupe.claim(key) is ClaimState.IN_FLIGHT

        assert await dedupe.renew(key, next_owner)
        assert 30 < await redis_client.ttl(lease_key) <= ttl_seconds
        assert await dedupe.complete(key, next_owner)
        assert not await redis_client.exists(lease_key)
        ttl = await redis_client.ttl(completed_key)
        assert COMPLETED_TTL_SECONDS - 5 <= ttl <= COMPLETED_TTL_SECONDS
        assert not await dedupe.complete(key, next_owner)
        await dedupe.release(key, next_owner)
        assert await dedupe.claim(key) is ClaimState.COMPLETED
    finally:
        await redis_client.delete(lease_key, completed_key)
