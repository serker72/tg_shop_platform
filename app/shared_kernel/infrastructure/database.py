"""Declarative base and naming conventions for the whole project.

Every model must inherit from :class:`Base` so that constraint names are
generated deterministically by the database naming convention below. Explicit
names are important for Alembic autogenerate: without them PostgreSQL invents
names that differ between environments and migrations start to churn.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

__all__ = ["NAMING_CONVENTION", "Base", "metadata"]

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
