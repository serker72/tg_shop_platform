"""``bot_worker`` component: broker with task subscribers + :class:`SchedulerRunner`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.observability.broker import build_rabbit_broker
from app.core.process import ServiceContext
from app.core.scheduler import (
    RabbitTaskPublisher,
    SchedulerCollector,
    SchedulerRunner,
    SchedulerStats,
    TaskRegistry,
)
from app.modules.bot_manager.tasks import register_proxy_tasks


@asynccontextmanager
async def scheduler_component(ctx: ServiceContext) -> AsyncIterator[None]:
    """Subscribers always consume; the runner ticks only with ``SCHEDULER_ENABLED=true``.

    Exit order: runner loops cancelled first, then the broker stops (in-flight handlers get
    ``BROKER_GRACEFUL_TIMEOUT_SECONDS``). Runner loops are supervised (``ctx.supervisor``).
    """
    settings = ctx.settings
    redis = await ctx.container.get(Redis)
    session_factory = await ctx.container.get(async_sessionmaker[AsyncSession])
    broker = build_rabbit_broker(settings)
    registry = TaskRegistry()
    register_proxy_tasks(
        registry, broker, settings=settings, session_factory=session_factory, redis=redis
    )
    stats = SchedulerStats()
    ctx.metrics.registry.register(SchedulerCollector(stats, registry.names))
    async with broker:
        await broker.start()
        if not settings.scheduler.scheduler_enabled:
            yield
            return
        runner = SchedulerRunner(
            registry,
            redis,
            RabbitTaskPublisher(broker),
            stats=stats,
            tick_seconds=settings.scheduler.scheduler_tick_seconds,
            heartbeat_ttl=settings.observability.heartbeat_ttl_seconds,
        )
        async with runner.running() as tasks:
            for task in tasks:
                ctx.supervisor.watch(task)
            yield
