"""Alembic environment for tg_shop_platform.

The database URL is never stored in ``alembic.ini``: it is built from
:class:`app.core.config.Settings` (``POSTGRES_*`` env variables / dotenv file) and handed
to an async engine (:mod:`sqlalchemy.ext.asyncio` with the ``psycopg`` v3 driver).

Every module's models must be imported before autogenerate, otherwise
``Base.metadata`` is incomplete and Alembic would emit ``DROP TABLE`` for live tables.
:func:`import_all_models` does that explicitly; modules that do not exist yet are skipped
safely (an *existing* module that fails to import propagates its exception).

Migrations are collected in one linear history (``migrations/versions``). Logging is
delegated to :func:`app.core.logging.configure_logging` instead of ``fileConfig`` so the
migration process shares the application log pipeline (stdout, single handler).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.migrations_support import import_all_models, include_name, include_object
from app.shared_kernel.infrastructure.database import Base

# Alembic Config object: access to the values in alembic.ini.
config = context.config

# Only fall back to the ini logging sections when they are actually present; the project
# configures logging in code (see the module docstring).
if config.config_file_name is not None and config.get_section("loggers") is not None:
    fileConfig(config.config_file_name)

# Model list, importer and autogenerate filters live in app.core.migrations_support
# (unit-tested there; this file is excluded from mypy and runs migrations on import).

#: Autogenerate target: every table declared through the shared declarative base.
target_metadata = Base.metadata


def _database_url() -> str:
    """Return the async ``postgresql+psycopg`` DSN from the application settings."""
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DBAPI connection (``alembic upgrade --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=False,
        include_schemas=True,
        include_name=include_name,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on an established (sync-facing) connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=False,
        include_schemas=True,
        include_name=include_name,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create the async engine and run migrations inside a connection."""
    # URL comes from settings, not from alembic.ini (there is no `sqlalchemy.url` there).
    section: dict[str, Any] = {"sqlalchemy.url": _database_url()}
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for the online mode: drives the async migration coroutine."""
    configure_logging(json_logs=False)
    asyncio.run(run_async_migrations())


import_all_models()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
