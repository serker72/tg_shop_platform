"""Master bot process entrypoint: ``python -m app.entrypoints.master_bot``."""

import asyncio

from app.core.process import run_service


def main() -> None:
    asyncio.run(run_service("master_bot"))


if __name__ == "__main__":
    main()
