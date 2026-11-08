"""Dishka provider of the IAM module.

Requires ``redis.asyncio.Redis`` (``RedisProvider``) and ``AsyncSession``
(``DatabaseProvider``).
"""

from __future__ import annotations

from dishka import Provider, Scope, provide
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam.cache import PermissionCache
from app.modules.iam.service import PermissionService

__all__ = ["IamProvider"]


class IamProvider(Provider):
    """``PermissionCache`` (APP) and ``PermissionService`` (REQUEST, request session)."""

    @provide(scope=Scope.APP)
    def provide_permission_cache(self, redis: Redis) -> PermissionCache:
        """Process-wide cache over the shared Redis client."""
        return PermissionCache(redis)

    @provide(scope=Scope.REQUEST)
    def provide_permission_service(
        self, session: AsyncSession, cache: PermissionCache
    ) -> PermissionService:
        """Permission checker bound to the request session."""
        return PermissionService(session, cache)
