"""``bot_worker`` component: broker with task subscribers + :class:`SchedulerRunner`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
    declare_bot_command_topology,
    register_bot_command_consumer,
)
from app.modules.bot_manager.commands import BOT_COMMAND_EXCHANGE, BOT_COMMAND_FANOUT
from app.modules.bot_manager.tasks import register_proxy_tasks
from app.shared_kernel.infrastructure.outbox import SharedOutboxRelay

BOT_RUNTIME_READINESS = "bot_runtime"


@asynccontextmanager
async def scheduler_component(
    ctx: ServiceContext, *, runtime_adapter: BotRuntimeCommandAdapter | None = None
) -> AsyncIterator[None]:
    """Run task subscribers independently of the optional bot command transport.

    The runner ticks only with ``SCHEDULER_ENABLED=true``. Without an adapter,
    publish commands to the durable RabbitMQ queue without consuming them.

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
    if runtime_adapter is None:
        structlog.get_logger(__name__).warning(
            "bot_command_consumer_paused",
            readiness=BOT_RUNTIME_READINESS,
            reason="runtime_adapter_unavailable",
        )
    stats = SchedulerStats()
    ctx.metrics.registry.register(SchedulerCollector(stats, registry.names))
    # RabbitBroker.__aenter__ only connects; it does not start subscribers.
    # Declare the DLQ and register the command consumer before broker.start(),
    # which is the point where FastStream activates delivery.
    async with broker:
        await declare_bot_command_topology(broker)
        relay = SharedOutboxRelay(
            outbox_broker=await ctx.container.get(OutboxBroker),
            rabbit_broker=broker,
            engine=await ctx.container.get(AsyncEngine),
            exchanges={BOT_COMMAND_EXCHANGE: BOT_COMMAND_FANOUT},
        )
        if runtime_adapter is not None:
            register_bot_command_consumer(broker, runtime_adapter, RedisCommandDeduplicator(redis))
        await broker.start()
        try:
            await relay.start()
            if relay.task is not None:
                ctx.supervisor.watch(relay.task)
            if runtime_gate is not None and runtime_adapter is not None:
                runtime_gate.set_ready()
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
            if runtime_gate is not None:
                runtime_gate.set_not_ready()
            await relay.stop()
