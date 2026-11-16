"""Drop every cached permission snapshot (``INCR perms:gen``), best-effort.

Usage (project root locally, ``/app`` in the container)::

    python -m app.entrypoints.cli.invalidate_permissions

Called by ``ops/scripts/migrate.sh`` after ``alembic upgrade head`` and the platform setup:
a migration may change the permission catalogue or role links, while snapshots cached in
Redis (:class:`~app.modules.iam.cache.PermissionCache`) would keep the old rights up to
their TTL. Uses the existing generation mechanism (``PermissionCache.invalidate_all``).

Best-effort: an unreachable Redis or a configuration error is logged as a warning and the
exit code is still 0 - the migration itself has succeeded.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.entrypoints.cli._common import EXIT_OK, first_line
from app.modules.iam.cache import PermissionCache
from app.shared_kernel.infrastructure.redis import build_redis

__all__ = ["EXIT_OK", "main", "run"]

logger = get_logger(__name__)


async def run(settings: Settings) -> bool:
    """``True`` if Redis confirmed the global generation bump."""
    redis = build_redis(settings)
    try:
        return (await PermissionCache(redis).invalidate_all()).ok
    finally:
        await redis.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; always returns 0 (best-effort step of ``migrate.sh``)."""
    parser = argparse.ArgumentParser(
        prog="python -m app.entrypoints.cli.invalidate_permissions",
        description="Invalidate all cached permission snapshots (best-effort).",
    )
    parser.parse_args(argv)
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        ok = asyncio.run(run(get_settings()))
    except Exception as error:  # noqa: BLE001 - best-effort, never fails the migration
        logger.warning(
            "permission_cache_invalidate_skipped",
            error_type=type(error).__name__,
            error=first_line(error),
        )
        return EXIT_OK
    if ok:
        logger.info("permission_cache_invalidated")
    else:
        logger.warning("permission_cache_invalidate_skipped", error="redis did not confirm")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
