"""Superuser installer CLI (R-II.1).

Usage (project root locally, ``/app`` in the container; ``make superuser`` on the stack)::

    python -m app.entrypoints.cli.superuser create --telegram-id N [--username U]
    python -m app.entrypoints.cli.superuser revoke --telegram-id N [--force]

``create`` checks the seeded permission catalogue (rc 1 "apply migrations" if incomplete),
then creates the user / restores a soft-deleted one / sets the flag - idempotently.
``revoke`` clears the flag; the last active superuser is kept unless ``--force``.
Every change is audited (``actor_type="system"``); the permission cache is invalidated after
the commit - Redis being down is only a warning (rc 0). Not called by ``migrate.sh`` (Q10).

Exit codes: 0 - success, 1 - runtime error, 2 - configuration/argument error.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence

from redis.asyncio import Redis
from sqlalchemy.exc import OperationalError

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.entrypoints.cli._common import EXIT_CONFIG, EXIT_OK, log_failure
from app.modules.iam.cache import PermissionCache
from app.modules.iam.service import PermissionService
from app.modules.iam.superuser import SuperuserInstaller, SuperuserResult
from app.shared_kernel.domain.exceptions import ConfigurationError, DomainError
from app.shared_kernel.infrastructure.database import build_engine, build_session_factory
from app.shared_kernel.infrastructure.redis import build_redis

__all__ = ["PROG", "build_parser", "main", "run"]

PROG = "python -m app.entrypoints.cli.superuser"

logger = get_logger(__name__)


def _telegram_id(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Create/restore or revoke a platform superuser."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create/restore an active superuser")
    create.add_argument("--telegram-id", dest="telegram_id", type=_telegram_id, required=True)
    create.add_argument("--username", default=None, help="Telegram username (optional)")
    revoke = commands.add_parser("revoke", help="clear the superuser flag")
    revoke.add_argument("--telegram-id", dest="telegram_id", type=_telegram_id, required=True)
    revoke.add_argument(
        "--force", action="store_true", help="allow revoking the last active superuser"
    )
    return parser


async def run(settings: Settings, args: argparse.Namespace) -> SuperuserResult:
    """Execute the parsed command; engine and Redis client are always closed."""
    if settings.postgres.password is None:
        raise ConfigurationError(
            "POSTGRES_PASSWORD is not configured", code="postgres_password_missing"
        )
    engine = build_engine(settings)
    redis: Redis | None = None
    try:
        redis = build_redis(settings)
        session_factory = build_session_factory(engine)
        async with session_factory() as session:
            installer = SuperuserInstaller(
                session, PermissionService(session, PermissionCache(redis))
            )
            if args.command == "create":
                return await installer.grant(args.telegram_id, args.username)
            return await installer.revoke(args.telegram_id, force=args.force)
    finally:
        try:
            if redis is not None:
                await redis.aclose()
        finally:
            await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the exit code."""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exit_:
        return EXIT_OK if exit_.code in (0, None) else EXIT_CONFIG
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        result = asyncio.run(run(get_settings(), args))
    except Exception as error:
        return log_failure(
            logger,
            "superuser_failed",
            error,
            quiet=(OperationalError, DomainError),
            command=args.command,
            telegram_user_id=args.telegram_id,
            error_code=getattr(error, "code", None),
        )
    if not result.cache_invalidated:
        logger.warning(
            "superuser_cache_invalidation_failed",
            telegram_user_id=result.telegram_user_id,
            hint="change is stored; cached permissions expire by TTL",
        )
    logger.info(
        "superuser_done",
        command=args.command,
        telegram_user_id=result.telegram_user_id,
        changed=result.changed,
        created=result.created,
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
