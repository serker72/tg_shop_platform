"""Declarative base and naming conventions for the whole project.

Every model must inherit from :class:`Base` so that constraint names are
generated deterministically by the database naming convention below. Explicit
names are important for Alembic autogenerate: without them PostgreSQL invents
names that differ between environments and migrations start to churn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import Pool, QueuePool

if TYPE_CHECKING:
    from app.core.config import Settings

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "build_engine",
    "build_session_factory",
    "metadata",
]

#: Naming convention for generated constraint / index names.
#:
#: ``column_0_N_name`` expands to all columns of the constraint joined by ``_``.
#: Checks use ``%(constraint_name)s``, therefore every ``CheckConstraint`` MUST
#: be declared with an explicit ``name=`` (it becomes the ``ck_<table>_<name>``
#: suffix). PostgreSQL truncates identifiers at 63 bytes, so keep table and
#: column names short - the prefix already costs ``ck_``/``fk_``/``ix_``/``uq_``.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Root declarative class shared by all models of the project."""

    metadata = metadata


def build_engine(settings: Settings, *, poolclass: type[Pool] | None = None) -> AsyncEngine:
    """Create the async ``postgresql+psycopg`` engine from the application settings.

    ``pool_size`` / ``max_overflow`` (``SQLALCHEMY_*``) are passed only to queue pools: the
    default async pool (``AsyncAdaptedQueuePool``) or an explicit ``QueuePool`` subclass.
    Other pools (``NullPool`` in tests, ``StaticPool``) reject these arguments.
    """
    kwargs: dict[str, Any] = {"echo": settings.sqlalchemy.echo, "pool_pre_ping": True}
    if poolclass is not None:
        kwargs["poolclass"] = poolclass
    if poolclass is None or issubclass(poolclass, QueuePool):
        kwargs["pool_size"] = settings.sqlalchemy.pool_size
        kwargs["max_overflow"] = settings.sqlalchemy.max_overflow
    return create_async_engine(settings.database_url, **kwargs)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the project-wide ``AsyncSession`` factory.

    ``expire_on_commit=False`` is mandatory: with expiring commits every attribute of a
    managed entity is expired after ``commit()``, and reloading it lazily is implicit IO,
    which async SQLAlchemy forbids (``MissingGreenlet``). ``BaseRepository.update`` inspects
    the loaded state without IO and would refuse such an entity with ``entity_state_unknown``.
    """
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=True)
