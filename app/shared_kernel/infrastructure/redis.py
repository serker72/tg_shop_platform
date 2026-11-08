"""Factory of the async Redis client (``redis.asyncio``).

* ``decode_responses=False``: values are opaque ``bytes`` (JSON payloads are encoded and
  decoded by their owners), so no implicit UTF-8 decoding happens on the hot path and
  binary values stay possible.
* ``socket_timeout`` / ``socket_connect_timeout`` = ``REDIS_SOCKET_TIMEOUT`` and **no
  retries**: Redis is a cache, a dead server must cost at most one timeout before the
  caller falls back to PostgreSQL (redis-py >= 6 retries 3 times with backoff by default).
* ``health_check_interval=30``: idle pooled connections are pinged before reuse, so a
  restarted Redis does not hand out dead sockets (checked lazily, no background thread).
* The password lives only in the connection kwargs; ``repr(client)`` shows host, port and
  db, never the password.
"""

from __future__ import annotations

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.core.config import Settings

__all__ = ["build_redis"]

#: Idle connection liveness probe interval, seconds.
HEALTH_CHECK_INTERVAL_SECONDS = 30


def build_redis(settings: Settings) -> Redis:
    """Create a client from ``settings.redis_url``; no connection is opened yet."""
    timeout = settings.redis.socket_timeout
    return Redis.from_url(
        settings.redis_url,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
        health_check_interval=HEALTH_CHECK_INTERVAL_SECONDS,
        decode_responses=False,
        retry=Retry(NoBackoff(), 0),
    )
