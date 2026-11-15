"""Master bot process entrypoint: ``python -m app.entrypoints.master_bot``."""

import asyncio

from app.core.process import run_service
from app.entrypoints.master_bot.app import master_bot_component


def main() -> None:
    asyncio.run(run_service("master_bot", components=(master_bot_component,)))


if __name__ == "__main__":
    main()
