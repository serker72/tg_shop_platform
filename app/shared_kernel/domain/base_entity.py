"""Base entity mixins shared by all business models.

Layering: this module is pure SQLAlchemy ORM (declarative mapping), it holds
no business logic and no I/O. Domain code uses entities by their attributes,
persistence concerns live in ``app.shared_kernel.infrastructure``.

Columns are declared with ``@declared_attr`` on purpose. All three mixins are
combined into ``BusinessEntity`` and then inherited by every model, and a
``mapped_column`` stored as a plain class attribute would be bound to the first
table it is copied into ("attribute ... already assigned to a different
table"). ``declared_attr`` builds a fresh ``Column`` per mapped class instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Uuid, false, func, true
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

__all__ = [
    "TIMESTAMP_MAPPER_ARGS",
    "BaseEntity",
    "BusinessEntity",
    "SoftDeleteMixin",
    "TimestampMixin",
]

#: Mapper options every timestamped entity needs. Subclasses that declare their own
#: ``__mapper_args__`` replace the mixin's directive, so they must merge this dict::
#:
#:     __mapper_args__ = {**TIMESTAMP_MAPPER_ARGS, "version_id_col": ...}
TIMESTAMP_MAPPER_ARGS: dict[str, Any] = {"eager_defaults": True}


class TimestampMixin:
    """Audit timestamps maintained by the database itself.

    ``created_at`` is set by the server on INSERT; ``updated_at`` on INSERT and
    refreshed by SQLAlchemy on every UPDATE (``onupdate`` is client-side, so
    bulk ``UPDATE`` statements bypass it - they must set the column explicitly).

    ``eager_defaults=True`` makes the flush fetch server-generated values with
    ``INSERT/UPDATE ... RETURNING``. Without it ``updated_at`` stays expired after an
    UPDATE and the next attribute access triggers lazy IO, which raises
    ``MissingGreenlet`` under ``AsyncSession``. A subclass may override
    ``__mapper_args__`` but must keep these options (see :data:`TIMESTAMP_MAPPER_ARGS`).
    """

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:
        return dict(TIMESTAMP_MAPPER_ARGS)

    @declared_attr
    @classmethod
    def created_at(cls) -> Mapped[datetime]:
        return mapped_column(
            DateTime(timezone=True),
            server_default=func.now(),
            nullable=False,
        )

    @declared_attr
    @classmethod
    def updated_at(cls) -> Mapped[datetime]:
        return mapped_column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        )


class BaseEntity(TimestampMixin):
    """Surrogate UUID key + audit timestamps, mixed into mapped classes.

    The class has no ``__tablename__`` and is never mapped by itself; it only
    contributes columns to a concrete model. The recommended style is to list the
    mixins first (``class Product(BusinessEntity, Base)``); the reverse order
    ``(Base, BusinessEntity)`` maps identically and is also correct.
    """

    @declared_attr
    @classmethod
    def id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            Uuid(as_uuid=True),
            primary_key=True,
            default=uuid.uuid4,
        )


class SoftDeleteMixin:
    """Soft-delete flags: rows are never physically deleted.

    * ``is_active`` - business-level switch (entity temporarily disabled);
    * ``is_deleted`` - tombstone set by :meth:`BaseRepository.soft_delete`.

    Both default to a value generated on the database side, so a row inserted
    by raw SQL also gets sensible defaults.
    """

    @declared_attr
    @classmethod
    def is_active(cls) -> Mapped[bool]:
        return mapped_column(
            Boolean,
            server_default=true(),
            nullable=False,
        )

    @declared_attr
    @classmethod
    def is_deleted(cls) -> Mapped[bool]:
        return mapped_column(
            Boolean,
            server_default=false(),
            nullable=False,
            index=True,
        )


class BusinessEntity(BaseEntity, SoftDeleteMixin):
    """Base for tenant business entities: UUID key, timestamps, soft delete.

    Usage::

        class Product(BusinessEntity, Base):
            __tablename__ = "product"

    Listing ``Base`` (``app.shared_kernel.infrastructure.database``) last is the
    recommended project style (mixins first, declarative base last); the order
    ``(Base, BusinessEntity)`` is mapped the same way and is not an error.
    """
