"""Bot worker process entrypoint: ``python -m app.entrypoints.bot_worker``."""

import asyncio

from app.core.process import run_placeholder


def main() -> None:
    asyncio.run(run_placeholder("bot_worker"))


if __name__ == "__main__":
    main()
