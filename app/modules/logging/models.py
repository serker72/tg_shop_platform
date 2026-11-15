"""Partitioned log tables (pg_partman, monthly ``RANGE (created_at)``).

Logs are immutable: no ``updated_at`` / soft delete, rows disappear only through the
pg_partman retention policy. ``id`` and ``created_at`` are produced by the application
(no server defaults). The partition key is part of the composite PK ``(id, created_at)``,
so no foreign keys reference these tables and they reference nothing (logical links only).
Free-text columns are ``Text`` (design-plan: text fields without size); Telegram
``callback_data`` is stored as ``Text`` too so nothing is truncated if limits change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, PrimaryKeyConstraint, Text
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.shared_kernel.infrastructure.database import Base

__all__ = ["ACTOR_TYPES", "PARTITION_BY", "AuditLog", "UserActivity", "utc_now"]

PARTITION_BY = "RANGE (created_at)"
#: Allowed ``audit_logs.actor_type`` values (DB CHECK ``ck_audit_logs_actor_type``).
ACTOR_TYPES: tuple[str, ...] = ("user", "system", "bot")


def utc_now() -> datetime:
    """Current UTC time; default of ``created_at`` (the partition key)."""
    return datetime.now(UTC)


class UserActivity(Base):
    """Click-log / User Activity (TZ 47.1-47.2, 47.10)."""

    __tablename__ = "user_activity"
    __table_args__ = (
        PrimaryKeyConstraint("id", "created_at"),
        Index("ix_user_activity_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_user_activity_telegram_user_id_created_at", "telegram_user_id", "created_at"),
        Index("ix_user_activity_telegram_bot_id_created_at", "telegram_bot_id", "created_at"),
        {"postgresql_partition_by": PARTITION_BY},
    )

    id: Mapped[UUID] = mapped_column(SAUuid, nullable=False, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    tenant_id: Mapped[UUID | None] = mapped_column(SAUuid, nullable=True)
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_id: Mapped[UUID | None] = mapped_column(SAUuid, nullable=True)
    order_id: Mapped[UUID | None] = mapped_column(SAUuid, nullable=True)
    product_id: Mapped[UUID | None] = mapped_column(SAUuid, nullable=True)
    category_id: Mapped[UUID | None] = mapped_column(SAUuid, nullable=True)
    subcategory_id: Mapped[UUID | None] = mapped_column(SAUuid, nullable=True)
    case_id: Mapped[UUID | None] = mapped_column(SAUuid, nullable=True)
    preorder_id: Mapped[UUID | None] = mapped_column(SAUuid, nullable=True)
    button: Mapped[str | None] = mapped_column(Text, nullable=True)
    callback_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    screen_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    screen_current: Mapped[str | None] = mapped_column(Text, nullable=True)
    screen_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    """Audit Log of critical actions (TZ 47.7, 47.9); never replaced by the click-log."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        PrimaryKeyConstraint("id", "created_at"),
        CheckConstraint(
            "actor_type IN (" + ", ".join(f"'{value}'" for value in ACTOR_TYPES) + ")",
            name="actor_type",
        ),
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_tenant_id_created_at", "tenant_id", "created_at"),
        {"postgresql_partition_by": PARTITION_BY},
    )

    id: Mapped[UUID] = mapped_column(SAUuid, nullable=False, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    tenant_id: Mapped[UUID | None] = mapped_column(SAUuid, nullable=True)
    telegram_bot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_id: Mapped[UUID | None] = mapped_column(SAUuid, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
