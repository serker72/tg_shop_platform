"""IAM reference models: tenants, their settings and the global language list."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    Uuid,
    false,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.shared_kernel.domain.base_entity import BaseEntity, BusinessEntity
from app.shared_kernel.infrastructure.database import Base

__all__ = [
    "DEFAULT_LANGUAGE_CODE",
    "LANGUAGE_CODE_PATTERN",
    "SEED_LANGUAGES",
    "TENANT_SLUG_MAX_LENGTH",
    "TENANT_SLUG_PATTERN",
    "Language",
    "Tenant",
    "TenantSettings",
]

#: Global default language; mirrored (as literals) by the ``iam_tenants_languages`` revision.
DEFAULT_LANGUAGE_CODE = "ru"
#: ``(code, name, is_default)`` rows seeded by the ``iam_tenants_languages`` revision.
SEED_LANGUAGES: tuple[tuple[str, str, bool], ...] = (
    ("ru", "Русский", True),
    ("en", "English", False),
    ("lv", "Latviešu", False),
)
#: Lower-case language tag like Telegram ``language_code``: ``ru``, ``pt-br``, ``es-419``.
LANGUAGE_CODE_PATTERN = r"^[a-z]{2,3}(-[a-z0-9]{2,8})?$"
#: Lower-case words separated by single hyphens: ``my-shop-1`` (no ``-x``, ``x-``, ``a--b``).
TENANT_SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
TENANT_SLUG_MAX_LENGTH = 64


class Tenant(BusinessEntity, Base):
    """Tenant (shop owner account). ``slug`` is a unique URL-safe identifier.

    ``slug`` is unique across *all* rows, soft-deleted included: a deleted tenant can
    only be brought back with ``restore``, its slug is never reused by another tenant.
    Soft-deleting a tenant leaves its ``TenantSettings`` untouched (the FK cascade fires
    only on a physical purge).
    """

    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(
            f"slug ~ '{TENANT_SLUG_PATTERN}' AND length(slug) <= {TENANT_SLUG_MAX_LENGTH}",
            name="slug_format",
        ),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class Language(BusinessEntity, Base):
    """Global language list.

    * At most one row has ``is_default`` (``ix_languages_single_default``) and that row is
      always live: CHECK ``default_is_live`` forbids soft-deleting or deactivating it.
      To retire the default language, first move ``is_default`` to another row in two
      steps: clear the flag on the old row, ``await session.flush()``, only then set it on
      the new row and flush again. The unique index is not deferrable and the unit of work
      orders UPDATEs of persistent rows by primary key (a random UUID), so changing both
      rows in one flush may hit the index on the intermediate state. A single
      ``UPDATE ... SET is_default = (code = :new)`` must not be used either (the index is
      checked row by row). Zero default rows are possible only through direct operations;
      readers then fall back to :data:`DEFAULT_LANGUAGE_CODE`.
    * ``code`` is stored in lower case, the way Telegram sends ``language_code``
      (``pt-br``). Resolution order for the future language service: exact match ->
      primary subtag (``pt-br`` -> ``pt``) -> global default -> ``DEFAULT_LANGUAGE_CODE``.
    * ``code`` is unique across *all* rows, soft-deleted included: a deleted language can
      only be brought back with ``restore``, not re-created.
    """

    __tablename__ = "languages"
    __table_args__ = (
        CheckConstraint(f"code ~ '{LANGUAGE_CODE_PATTERN}'", name="code_format"),
        CheckConstraint("NOT is_default OR (is_active AND NOT is_deleted)", name="default_is_live"),
        Index(
            "ix_languages_single_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )


class TenantSettings(BaseEntity, Base):
    """Per-tenant settings, exactly one row per tenant (no soft delete).

    * ``default_language_code`` empty -> the global ``Language.is_default`` row is used.
      ``ON DELETE RESTRICT`` guards only a *physical* ``DELETE`` of the language; blocking
      soft-delete/deactivation of a language in use and the fallback for a dead language
      are the job of the future service.
    * ``ON DELETE CASCADE`` fires only on a physical purge of the tenant. A soft-deleted
      tenant keeps its settings row, so settings queries must join ``tenants`` and filter
      ``tenants.is_deleted``.
    * Creating ``TenantSettings`` together with a ``Tenant`` is the job of a future
      service; this module only declares the model.

    Python ``default=`` values mirror ``server_default`` (ORM inserts send the former).
    """

    __tablename__ = "tenant_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    is_store_open: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    is_buy_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    is_refill_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    is_referral_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    is_notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    default_language_code: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("languages.code", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    support_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    news_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    faq: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
