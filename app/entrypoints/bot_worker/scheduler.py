"""``bot_worker`` component: broker with task subscribers + :class:`SchedulerRunner`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from faststream_outbox import OutboxBroker
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.observability.broker import build_rabbit_broker
from app.core.process import ServiceContext
from app.core.scheduler import (
    RabbitTaskPublisher,
    SchedulerCollector,
    SchedulerRunner,
    SchedulerStats,
    TaskRegistry,
)
from app.entrypoints.bot_worker.commands import (
    BotRuntimeCommandAdapter,
    RedisCommandDeduplicator,
    declare_bot_command_dlq,
    register_bot_command_consumer,
)
from app.modules.bot_manager.tasks import register_proxy_tasks
from app.shared_kernel.infrastructure.outbox import SharedOutboxRelay

BOT_RUNTIME_READINESS = "bot_runtime"


class _RuntimeAdapterUnavailable(BotRuntimeCommandAdapter):
    """Explicit task-10/11 seam; never reports success before runtime exists."""

    async def start(self, command: Any) -> None:
        raise RuntimeError("bot runtime adapter is not configured")

    async def stop(self, command: Any) -> None:
        raise RuntimeError("bot runtime adapter is not configured")

    async def restart(self, command: Any) -> None:
        raise RuntimeError("bot runtime adapter is not configured")


@asynccontextmanager
async def scheduler_component(ctx: ServiceContext) -> AsyncIterator[None]:
    """Subscribers always consume; the runner ticks only with ``SCHEDULER_ENABLED=true``.

    Exit order: runner loops cancelled first, then the broker stops (in-flight handlers get
    ``BROKER_GRACEFUL_TIMEOUT_SECONDS``). Runner loops are supervised (``ctx.supervisor``).
    """
    settings = ctx.settings
    redis = await ctx.container.get(Redis)
    session_factory = await ctx.container.get(async_sessionmaker[AsyncSession])
    outbox_broker = await ctx.container.get(OutboxBroker)
    engine = await ctx.container.get(AsyncEngine)
    broker = build_rabbit_broker(settings)
    registry = TaskRegistry()
    register_proxy_tasks(
        registry, broker, settings=settings, session_factory=session_factory, redis=redis
    )
    runtime_ready = ctx.readiness_registry
    runtime_gate = (
        runtime_ready.readiness.get(BOT_RUNTIME_READINESS) if runtime_ready is not None else None
    )
    if runtime_gate is None:
        runtime_gate = (
            runtime_ready.register_readiness(BOT_RUNTIME_READINESS) if runtime_ready else None
        )
    if runtime_gate is not None:
        runtime_gate.set_not_ready()
    production = getattr(settings, "project_environment", "test") == "prod"
    stats = SchedulerStats()
    ctx.metrics.registry.register(SchedulerCollector(stats, registry.names))
    # RabbitBroker.__aenter__ only connects; it does not start subscribers.
    # Declare the DLQ and register the command consumer before broker.start(),
    # which is the point where FastStream activates delivery.
    async with broker:
        await declare_bot_command_dlq(broker)
        # Registration is deliberately unconditional: the production broker topology
        # must expose the durable consumer even when task-10/11 runtime is unavailable.
        register_bot_command_consumer(
            broker,
            _RuntimeAdapterUnavailable(),
            RedisCommandDeduplicator(redis),
        )
        if production:
            # Fail before broker.start() and relay activation.  Keep the readiness gate
            # not-ready and emit a durable diagnostic: a healthy process with a silently
            # disabled consumer would allow outbox rows to be published/deleted forever.
            structlog.get_logger(__name__).critical(
                "bot_runtime_unavailable_startup_blocked", readiness=BOT_RUNTIME_READINESS
            )
            raise RuntimeError("bot runtime adapter is not configured; startup blocked")
        await broker.start()
        relay: SharedOutboxRelay | None = None
        if not production:
            relay = SharedOutboxRelay(
                outbox_broker=outbox_broker, rabbit_broker=broker, engine=engine
            )
            await relay.start()
            if relay.task is not None:
                ctx.supervisor.watch(relay.task)
        try:
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
        finally:
            if relay is not None:
                await relay.stop()
