"""``bot_worker`` component: broker with task subscribers + :class:`SchedulerRunner`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

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
    InMemoryCommandDeduplicator,
    declare_bot_command_topology,
    register_bot_command_consumer,
)
from app.modules.bot_manager.commands import BOT_COMMAND_EXCHANGE, BOT_COMMAND_FANOUT
from app.modules.bot_manager.tasks import register_proxy_tasks
from app.modules.bot_manager.worker import BotWorker
from app.shared_kernel.infrastructure.outbox import SharedOutboxRelay

BOT_RUNTIME_READINESS = "bot_runtime"


@asynccontextmanager
async def scheduler_component(
    ctx: ServiceContext, *, worker: BotWorker | None = None
) -> AsyncIterator[None]:
    """Run task subscribers independently of the optional bot command transport.

    Without a worker the scheduler and relay still run; unroutable commands
    stay in the SQL outbox for retry. Both transports share one broker.
    """
    settings = ctx.settings
    redis = await ctx.container.get(Redis)
    session_factory = await ctx.container.get(async_sessionmaker[AsyncSession])

    registry = TaskRegistry()
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
    if worker is None:
        structlog.get_logger(__name__).warning(
            "bot_command_consumer_paused",
            readiness=BOT_RUNTIME_READINESS,
            reason="runtime_adapter_unavailable",
        )
    # aio-pika calls on_reconnected after robust channels restore queues,
    # bindings and consumers; TCP connectivity alone is insufficient.
    connected = True
    initialized = False
    closing = False

    def update_gate() -> None:
        if runtime_gate is not None:
            if connected and initialized and not closing:
                runtime_gate.set_ready()
            else:
                runtime_gate.set_not_ready()

    def disconnected(_: object, _error: BaseException | None) -> None:
        nonlocal connected
        connected = False
        update_gate()

    def restored(_: object) -> None:
        nonlocal connected
        connected = True
        update_gate()

    broker = (
        build_rabbit_broker(settings, on_close=disconnected, on_reconnected=restored)
        if worker is not None
        else build_rabbit_broker(settings)
    )
    register_proxy_tasks(
        registry, broker, settings=settings, session_factory=session_factory, redis=redis
    )
    stats = SchedulerStats()
    ctx.metrics.registry.register(SchedulerCollector(stats, registry.names))
    # RabbitBroker.__aenter__ only connects; it does not start subscribers.
    # Declare the DLQ and register the command consumer before broker.start(),
    # which is the point where FastStream activates delivery.
    async with AsyncExitStack() as broker_stack:
        if worker is None:
            await broker_stack.enter_async_context(broker)
        else:

            async def close_broker(*_: object) -> None:
                async with asyncio.timeout(18):
                    await broker.stop()

            # Register before connect, including cancellation during the first connection.
            broker_stack.push_async_exit(close_broker)
            await broker.connect()
        await declare_bot_command_topology(broker)
        relay = SharedOutboxRelay(
            outbox_broker=await ctx.container.get(OutboxBroker),
            rabbit_broker=broker,
            engine=await ctx.container.get(AsyncEngine),
            exchanges={BOT_COMMAND_EXCHANGE: BOT_COMMAND_FANOUT},
        )
        renew = reconcile = None
        if worker is not None:
            register_bot_command_consumer(broker, worker, InMemoryCommandDeduplicator())
            renew = asyncio.create_task(worker.renew_loop(), name="bot-lease-renew")
            ctx.supervisor.watch(renew)
        try:
            await broker.start()
            if worker is not None:
                await worker.reconcile(strict=True)
                reconcile = asyncio.create_task(worker.reconcile_loop(), name="bot-reconcile")
                ctx.supervisor.watch(reconcile)
                initialized = True
                update_gate()
            await relay.start()
            if relay.task is not None:
                ctx.supervisor.watch(relay.task)
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
            async with AsyncExitStack() as scheduler_stack:
                tasks = await scheduler_stack.enter_async_context(runner.running())
                for task in tasks:
                    ctx.supervisor.watch(task)
                try:
                    yield
                finally:
                    closing = True
                    update_gate()
                    if worker is not None:
                        worker.close_admission()
                    async with asyncio.timeout(3):
                        await scheduler_stack.aclose()
        finally:
            closing = True
            update_gate()
            if worker is not None:
                worker.close_admission()
            try:
                async with asyncio.timeout(12):
                    if reconcile is not None:
                        reconcile.cancel()
                        await asyncio.gather(reconcile, return_exceptions=True)
                    if worker is not None:
                        await worker.shutdown()
            finally:
                if renew is not None:
                    renew.cancel()
                async with asyncio.timeout(3):
                    await asyncio.gather(
                        relay.stop(),
                        *((renew,) if renew is not None else ()),
                        return_exceptions=True,
                    )
