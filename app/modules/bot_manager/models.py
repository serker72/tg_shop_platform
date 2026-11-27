"""Bot manager models: shop bots and the customers of a shop.

* ``bots`` holds **shop bots only**; the Master Bot is configured through settings and is
  never stored here (decisions.md, 2026-11-15). ``id`` (UUID) is the internal key used by
  commands, leases and foreign keys; ``telegram_bot_id`` is the Telegram ID of the bot
  (``getMe().id``) and is unique across all rows, soft-deleted included: adding the same bot
  again means ``restore``, never a second row. Logs reference bots by ``telegram_bot_id``
  (``user_activity`` / ``audit_logs``, no FK: the log tables are partitioned).
* ``customers`` - a Telegram user known to a tenant (shop), one row per pair.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.shared_kernel.domain.base_entity import BaseEntity, BusinessEntity
from app.shared_kernel.infrastructure.database import Base
from app.shared_kernel.infrastructure.encrypted_string import EncryptedString
from app.shared_kernel.infrastructure.iana_timezone import IanaTimezone
from app.shared_kernel.timezones import validate_timezone

__all__ = [
    "BOT_STATUSES",
    "BOT_STATUS_ERROR",
    "BOT_STATUS_INVALID_TOKEN",
    "BOT_STATUS_RUNNING",
    "BOT_STATUS_STARTING",
    "BOT_STATUS_STOPPED",
    "PROXY_STATUSES",
    "PROXY_STATUS_ACTIVE",
    "PROXY_STATUS_BANNED",
    "PROXY_STATUS_QUARANTINE",
    "Bot",
    "Proxy",
    "Customer",
]

BOT_STATUS_STOPPED: Final = "stopped"
BOT_STATUS_STARTING: Final = "starting"
BOT_STATUS_RUNNING: Final = "running"
BOT_STATUS_INVALID_TOKEN: Final = "invalid_token"  # noqa: S105 - status name, not a secret
BOT_STATUS_ERROR: Final = "error"
#: Allowed ``bots.status`` values (DB CHECK ``ck_bots_status``).
BOT_STATUSES: tuple[str, ...] = (
    BOT_STATUS_STOPPED,
    BOT_STATUS_STARTING,
    BOT_STATUS_RUNNING,
    BOT_STATUS_INVALID_TOKEN,
    BOT_STATUS_ERROR,
)

PROXY_STATUS_ACTIVE: Final = "active"
PROXY_STATUS_QUARANTINE: Final = "quarantine"
PROXY_STATUS_BANNED: Final = "banned"
#: Allowed ``proxies.status`` values (DB CHECK ``ck_proxies_status``).
PROXY_STATUSES: tuple[str, ...] = (
    PROXY_STATUS_ACTIVE,
    PROXY_STATUS_QUARANTINE,
    PROXY_STATUS_BANNED,
)


class Bot(BusinessEntity, Base):
    """Telegram bot of a shop (tenant). The token is stored only encrypted."""

    __tablename__ = "bots"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in BOT_STATUSES) + ")",
            name="status",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_token: Mapped[str] = mapped_column(EncryptedString(), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    restart_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=BOT_STATUS_STOPPED, index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: Proxy of the bot (``ProxyPoolService``); ``NULL`` - not assigned / direct connection.
    proxy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("proxies.id", ondelete="SET NULL"), nullable=True, index=True
    )

    def __repr__(self) -> str:
        # Never include ``encrypted_token`` / ``token_hash``: reprs end up in logs.
        return (
            f"Bot(id={self.id!s}, tenant_id={self.tenant_id!s}, "
            f"telegram_bot_id={self.telegram_bot_id!r}, username={self.username!r}, "
            f"status={self.status!r})"
        )


class Proxy(BusinessEntity, Base):
    """Outgoing proxy of the bot pool (decisions.md, "Пул прокси").

    ``url`` keeps the credentials and is stored only encrypted; ``url_hash`` (sha256 of the
    normalized URL) makes it unique. The number of bots of a proxy is never stored: it is
    ``COUNT(bots WHERE proxy_id = id AND NOT is_deleted)``. ``last_ban_at`` - the moment the
    proxy went to quarantine (or was banned).
    """

    __tablename__ = "proxies"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in PROXY_STATUSES) + ")",
            name="status",
        ),
        CheckConstraint("strike_count >= 0", name="strike_count_non_negative"),
    )

    url: Mapped[str] = mapped_column(EncryptedString(), nullable=False)
    url_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=PROXY_STATUS_ACTIVE, index=True
    )
    strike_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_strike_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ban_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        # Never include ``url`` / ``url_hash``: the URL carries the proxy credentials.
        return (
            f"Proxy(id={self.id!s}, label={self.label!r}, status={self.status!r}, "
            f"strike_count={self.strike_count!r})"
        )


class Customer(BaseEntity, Base):
    """Telegram user of a shop; ``first_bot_id`` - the bot the user came through first."""

    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("tenant_id", "telegram_user_id"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str | None] = mapped_column(IanaTimezone(), nullable=True)
    language_code: Mapped[str | None] = mapped_column(
        Text, ForeignKey("languages.code", ondelete="RESTRICT"), nullable=True, index=True
    )
    first_bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="SET NULL"), nullable=True, index=True
    )

    @validates("timezone")
    def _validate_timezone(self, _key: str, value: str | None) -> str | None:
        return None if value is None else validate_timezone(value)

    def __repr__(self) -> str:
        return (
            f"Customer(id={self.id!s}, tenant_id={self.tenant_id!s}, "
            f"telegram_user_id={self.telegram_user_id!r})"
        )
