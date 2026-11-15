"""Periodic tasks of the bot manager: ``proxy.rehabilitate`` (daily, ``PROXY_REHABILITATE_AT``)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import structlog

from app.core.scheduler import Schedule, ScheduledTask, TaskRegistry, task_queue
from app.modules.bot_manager.proxy_health import ProxyHealthTracker
from app.modules.bot_manager.proxy_service import ProxyPoolService

if TYPE_CHECKING:
    from faststream.rabbit import RabbitBroker
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.config import ProxySettings, Settings

REHABILITATE_TASK: Final = "proxy.rehabilitate"

_log = structlog.get_logger(__name__)


async def rehabilitate_proxies(
    session_factory: async_sessionmaker[AsyncSession],
    settings: ProxySettings,
    redis: Any,
    *,
    now: datetime | None = None,
) -> int:
    """Quarantine older than ``PROXY_QUARANTINE_DAYS`` -> active; idempotent (one UPDATE)."""
    async with session_factory() as session:
        tracker = ProxyHealthTracker(redis, window_seconds=settings.proxy_strike_window_seconds)
        return await ProxyPoolService(session, settings, tracker).rehabilitate(now)


def register_proxy_tasks(
    registry: TaskRegistry,
    broker: RabbitBroker,
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
) -> ScheduledTask:
    """Register ``proxy.rehabilitate`` (catch_up) and its competing-consumer subscriber."""
    from faststream import AckPolicy
    from faststream.rabbit import RabbitQueue

    task = registry.register(
        ScheduledTask(
            name=REHABILITATE_TASK,
            schedule=Schedule.daily_at(settings.scheduler.proxy_rehabilitate_at),
            queue=task_queue(REHABILITATE_TASK),
            catch_up=True,
        )
    )

    async def handle_rehabilitate(body: dict[str, Any]) -> None:
        try:
            count = await rehabilitate_proxies(session_factory, settings.proxy, redis)
        except Exception:
            # REJECT_ON_ERROR: no redelivery loop; the next daily slot (or catch_up on the
            # next start) repeats the idempotent UPDATE.
            _log.exception(
                "scheduled_task_failed",
                task=REHABILITATE_TASK,
                scheduled_at=body.get("scheduled_at"),
            )
            raise
        _log.info(
            "scheduled_task_done",
            task=REHABILITATE_TASK,
            scheduled_at=body.get("scheduled_at"),
            rehabilitated=count,
        )

    broker.subscriber(RabbitQueue(task.queue, durable=True), ack_policy=AckPolicy.REJECT_ON_ERROR)(
        handle_rehabilitate
    )
    return task
