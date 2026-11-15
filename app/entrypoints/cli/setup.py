"""Platform setup on start: ensure the ``system_settings`` singleton row exists.

Usage (project root locally, ``/app`` in the container)::

    python -m app.entrypoints.cli.setup

Called by ``ops/scripts/migrate.sh`` after ``alembic upgrade head`` and before the
pg_partman retention sync. ``INSERT ... ON CONFLICT DO NOTHING``: values already stored in
the row are never overwritten.

Exit codes: 0 - success, 2 - configuration error, 1 - other errors.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.entrypoints.cli._common import (
    EXIT_CONFIG,
    EXIT_ERROR,
    EXIT_OK,
    exit_code_for,
    log_failure,
)
from app.modules.system.repository import SystemSettingsRepository
from app.shared_kernel.domain.exceptions import ConfigurationError
from app.shared_kernel.infrastructure.database import build_engine, build_session_factory

__all__ = ["EXIT_CONFIG", "EXIT_ERROR", "EXIT_OK", "exit_code_for", "main", "run"]

logger = get_logger(__name__)


async def run(settings: Settings) -> bool:
    """Ensure the ``system_settings`` row in one transaction; ``True`` if it was inserted.

    Raises :class:`ConfigurationError` (exit code 2) before connecting when
    ``POSTGRES_PASSWORD`` is not configured.
    """
    if settings.postgres.password is None:
        raise ConfigurationError(
            "POSTGRES_PASSWORD is not configured", code="postgres_password_missing"
        )
    engine = build_engine(settings)
    try:
        session_factory = build_session_factory(engine)
        async with session_factory() as session, session.begin():
            return await SystemSettingsRepository(session).ensure_exists()
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m app.entrypoints.cli.setup",
        description="Ensure the system_settings singleton row exists (never overwrites).",
    )
    parser.parse_args(argv)
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        inserted = asyncio.run(run(get_settings()))
    except Exception as error:
        return log_failure(logger, "platform_setup_failed", error)
    logger.info("platform_setup_done", inserted=inserted)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
