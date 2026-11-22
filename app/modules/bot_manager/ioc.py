"""Dishka providers for bot manager use cases and RabbitMQ commands."""

from __future__ import annotations

from dishka import Provider, Scope, provide
from faststream_outbox import OutboxBroker
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.modules.bot_manager.proxy_health import ProxyHealthTracker
from app.modules.bot_manager.proxy_service import ProxyPoolService
from app.modules.bot_manager.publisher import BotCommandPublisher
from app.modules.bot_manager.service import BotControlService
from app.modules.iam.service import PermissionService
from app.shared_kernel.infrastructure.outbox import build_outbox_broker


class BotManagerProvider(Provider):
    """Outbox and request-scoped bot control services."""

    @provide(scope=Scope.APP)
    def provide_outbox_broker(self, engine: AsyncEngine) -> OutboxBroker:
        return build_outbox_broker(engine)

    @provide(scope=Scope.REQUEST)
    def provide_publisher(self, outbox_broker: OutboxBroker) -> BotCommandPublisher:
        return BotCommandPublisher(outbox_broker)

    @provide(scope=Scope.REQUEST)
    def provide_proxy_health_tracker(self, redis: Redis, settings: Settings) -> ProxyHealthTracker:
        return ProxyHealthTracker(redis, window_seconds=settings.proxy.proxy_strike_window_seconds)

    @provide(scope=Scope.REQUEST)
    def provide_proxy_pool_service(
        self,
        session: AsyncSession,
        settings: Settings,
        tracker: ProxyHealthTracker,
        permissions: PermissionService,
    ) -> ProxyPoolService:
        return ProxyPoolService(session, settings.proxy, tracker, permissions=permissions)

    @provide(scope=Scope.REQUEST)
    def provide_bot_control_service(
        self,
        session: AsyncSession,
        permissions: PermissionService,
        proxy_pool: ProxyPoolService,
        publisher: BotCommandPublisher,
        settings: Settings,
    ) -> BotControlService:
        return BotControlService(
            session,
            permissions=permissions,
            proxy_pool=proxy_pool,
            publisher=publisher,
            tenants_use_proxy=settings.proxy.tenants_use_proxy,
        )


__all__ = ["BotManagerProvider"]
