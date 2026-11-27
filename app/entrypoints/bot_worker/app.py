"""Composition root for polling workers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram import Dispatcher
from prometheus_client import Counter
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.observability.tracing import AiogramTracingMiddleware
from app.core.process import ServiceContext
from app.entrypoints.bot_worker.scheduler import scheduler_component
from app.modules.bot_manager.runtime import BotSupervisor
from app.modules.bot_manager.worker import BotWorker
from app.modules.logging.ports import ActivityLogger


@asynccontextmanager
async def bot_worker_component(ctx: ServiceContext) -> AsyncIterator[None]:
    # Resolve the app-scoped buffer now; the container drains it after the broker.
    await ctx.container.get(ActivityLogger)
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(AiogramTracingMiddleware())
    counter = Counter(
        "bots_no_proxy_total",
        "Bot starts blocked by an exhausted proxy pool",
        registry=ctx.metrics.registry,
    )
    worker = BotWorker(
        ctx.settings,
        await ctx.container.get(Redis),
        await ctx.container.get(async_sessionmaker[AsyncSession]),
        BotSupervisor.from_settings(ctx.settings, metrics=ctx.metrics),
        dispatcher,
        on_no_proxy=counter.inc,
    )
    async with scheduler_component(ctx, worker=worker):
        yield
