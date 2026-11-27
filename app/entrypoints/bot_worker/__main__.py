"""Bot worker process entrypoint: ``python -m app.entrypoints.bot_worker``."""

import asyncio

from app.core.config import get_settings
from app.core.process import run_service
from app.core.scheduler import SCHEDULER_HEARTBEAT
from app.entrypoints.bot_worker.app import bot_worker_component
from app.entrypoints.bot_worker.scheduler import BOT_RUNTIME_READINESS


def main() -> None:
    enabled = get_settings().scheduler.scheduler_enabled
    asyncio.run(
        run_service(
            "bot_worker",
            components=(bot_worker_component,),
            # Non-critical: a missing scheduler beat degrades readiness, never fails it.
            heartbeats={SCHEDULER_HEARTBEAT: False} if enabled else None,
            readiness=(BOT_RUNTIME_READINESS,),
        )
    )


if __name__ == "__main__":
    main()
