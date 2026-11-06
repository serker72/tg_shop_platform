"""Platform-wide settings stored as a single row (``id = 1``)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Integer, false, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.shared_kernel.domain.base_entity import TimestampMixin
from app.shared_kernel.infrastructure.database import Base

__all__ = [
    "DEFAULT_AUDIT_LOGS_RETENTION_MONTHS",
    "DEFAULT_IAM_CACHE_TTL_SECONDS",
    "DEFAULT_USER_ACTIVITY_RETENTION_MONTHS",
    "RETENTION_MONTHS_MAX",
    "RETENTION_MONTHS_MIN",
    "SINGLETON_ID",
    "SystemSettings",
]

SINGLETON_ID = 1
DEFAULT_IAM_CACHE_TTL_SECONDS = 60 * 60 * 24
#: Defaults mirrored (as literals) by the ``system_settings`` / ``partition_logs`` revisions.
DEFAULT_USER_ACTIVITY_RETENTION_MONTHS = 3
DEFAULT_AUDIT_LOGS_RETENTION_MONTHS = 24
RETENTION_MONTHS_MIN = 1
RETENTION_MONTHS_MAX = 120


class SystemSettings(TimestampMixin, Base):
    """Singleton row: the CHECK ``id = 1`` makes a second row impossible.

    Built on ``TimestampMixin`` instead of :class:`BaseEntity`, because ``BaseEntity``
    brings a UUID key while this table has the fixed integer key ``1``. The row is inserted
    by the migration.
    ``*_retention_months`` are the source of truth for ``partman.part_config.retention``
    (applied by ``ops/scripts/sync_partman_retention.py``).
    """

    __tablename__ = "system_settings"
    __table_args__ = (
        CheckConstraint(f"id = {SINGLETON_ID}", name="singleton"),
        CheckConstraint("iam_cache_ttl_seconds > 0", name="iam_cache_ttl_positive"),
        CheckConstraint(
            f"user_activity_retention_months BETWEEN {RETENTION_MONTHS_MIN} "
            f"AND {RETENTION_MONTHS_MAX}",
            name="user_activity_retention_range",
        ),
        CheckConstraint(
            f"audit_logs_retention_months BETWEEN {RETENTION_MONTHS_MIN} "
            f"AND {RETENTION_MONTHS_MAX}",
            name="audit_logs_retention_range",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False, default=SINGLETON_ID
    )
    is_maintenance_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    iam_cache_ttl_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_IAM_CACHE_TTL_SECONDS,
        server_default=text(str(DEFAULT_IAM_CACHE_TTL_SECONDS)),
    )
    extra_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    user_activity_retention_months: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_USER_ACTIVITY_RETENTION_MONTHS,
        server_default=text(str(DEFAULT_USER_ACTIVITY_RETENTION_MONTHS)),
    )
    audit_logs_retention_months: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_AUDIT_LOGS_RETENTION_MONTHS,
        server_default=text(str(DEFAULT_AUDIT_LOGS_RETENTION_MONTHS)),
    )
