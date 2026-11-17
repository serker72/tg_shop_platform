"""Redis lock of one slot: ``SET sched:lock:<task>:<slot ISO UTC>Z <token> NX EX <ttl>``."""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Final

import structlog

from app.core.scheduler.schedule import slot_label
from app.shared_kernel.infrastructure.generational_cache import REDIS_ERRORS
from app.shared_kernel.infrastructure.redis_scripts import compare_and_delete

LOCK_KEY_PREFIX: Final = "sched:lock"

_log = structlog.get_logger(__name__)


def lock_key(task: str, slot: datetime) -> str:
    """Key of the lock of ``task`` for the planned fire time ``slot``."""
    return f"{LOCK_KEY_PREFIX}:{task}:{slot_label(slot)}"


class SlotLock:
    """One lock per (task, slot); the owner is identified by a random token."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def acquire(self, task: str, slot: datetime, ttl_seconds: int) -> str | None:
        """Token if this caller owns the slot, ``None`` if it is taken.

        Redis errors (connection, ``OOM command not allowed`` under ``noeviction``) are
        raised: the caller fails closed.
        """
        token = secrets.token_hex(16)
        acquired = await self._redis.set(lock_key(task, slot), token, nx=True, ex=ttl_seconds)
        return token if acquired else None

    async def release(self, task: str, slot: datetime, token: str) -> bool:
        """Best effort: drop the lock while it still holds ``token``."""
        try:
            # Compare-and-delete: never remove a key re-taken by someone else after expiry.
            return await compare_and_delete(self._redis, lock_key(task, slot), token)
        except REDIS_ERRORS as error:
            _log.warning(
                "scheduler_lock_release_failed",
                task=task,
                scheduled_at=slot_label(slot),
                error=type(error).__name__,
            )
            return False
