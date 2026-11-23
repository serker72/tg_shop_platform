"""IAM models: tenants, their settings, the global language list and RBAC tables."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.shared_kernel.domain.base_entity import BaseEntity, BusinessEntity
from app.shared_kernel.infrastructure.database import Base
from app.shared_kernel.infrastructure.iana_timezone import IanaTimezone
from app.shared_kernel.timezones import validate_timezone

__all__ = [
    "DEFAULT_LANGUAGE_CODE",
    "LANGUAGE_CODE_PATTERN",
    "PERMISSION_SLUG_PATTERN",
    "ROLE_SLUG_PATTERN",
    "SEED_LANGUAGES",
    "TENANT_SLUG_MAX_LENGTH",
    "TENANT_SLUG_PATTERN",
    "Language",
    "Permission",
    "Role",
    "RolePermission",
    "Tenant",
    "TenantModerator",
    "TenantSettings",
    "User",
    "UserPermissionOverride",
    "UserRole",
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
#: ``{module}.{action}``: ``products.view``, ``tenants.view_all``.
PERMISSION_SLUG_PATTERN = r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"
#: Lower-case snake case: ``senior_moderator``.
ROLE_SLUG_PATTERN = r"^[a-z][a-z0-9_]*$"


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
    timezone: Mapped[str] = mapped_column(IanaTimezone(), nullable=False, server_default="UTC")

    @validates("timezone")
    def _validate_timezone(self, _key: str, value: str) -> str:
        return validate_timezone(value)


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


# --- RBAC --------------------------------------------------------------------------------


class Permission(BusinessEntity, Base):
    """Permission ``{module}.{action}`` (see :mod:`app.modules.iam.permissions`).

    ``slug`` is unique across *all* rows, soft-deleted included (bring back with
    ``restore``). A physical ``DELETE`` cascades to ``role_permissions`` and
    ``user_permission_overrides``. The permission resolver must ignore permissions that
    are not ``is_active AND NOT is_deleted`` (soft delete keeps their links).
    """

    __tablename__ = "permissions"
    __table_args__ = (CheckConstraint(f"slug ~ '{PERMISSION_SLUG_PATTERN}'", name="slug_format"),)

    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class Role(BusinessEntity, Base):
    """Global role: a named set of permissions.

    * ``slug`` is unique across *all* rows, soft-deleted included (``restore`` only).
    * ``is_system`` roles (seeded ``admin`` / ``senior_moderator`` / ``moderator``) are
      always live: CHECK ``system_is_live`` forbids deactivating or soft-deleting them.
    * A physical ``DELETE`` of any role that is still assigned is blocked by
      ``ON DELETE RESTRICT`` of ``user_roles``. The service forbids (the database does
      not): physically deleting an unassigned system role (a CHECK can not see
      ``DELETE``), changing the ``slug`` of a system role and changing ``is_system``.
    * The permission resolver must ignore roles that are not
      ``is_active AND NOT is_deleted`` (their ``user_roles`` rows stay in place).
    """

    __tablename__ = "roles"
    __table_args__ = (
        CheckConstraint(f"slug ~ '{ROLE_SLUG_PATTERN}'", name="slug_format"),
        CheckConstraint("NOT is_system OR (is_active AND NOT is_deleted)", name="system_is_live"),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )


class RolePermission(Base):
    """Role <-> permission association (composite PK, no surrogate id, no timestamps).

    Rows are only inserted and deleted, never updated, so ``updated_at`` would be
    meaningless; who/when changed a role is the audit log's job. Without timestamps the
    table has no server-generated values, so ``eager_defaults`` is not needed.
    ``role_id`` lookups use the primary key, ``permission_id`` has its own index.
    """

    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )


class User(BusinessEntity, Base):
    """Platform staff user identified by the Telegram user id.

    * ``telegram_user_id`` is unique across *all* rows, soft-deleted included.
    * ``telegram_user_name`` is the Telegram username without ``@``; its format is not
      checked (Telegram owns it, it may change or disappear).
    * ``is_superuser`` bypasses every permission check and is set only by the CLI
      installer, never through the bot/API.
    * Soft-deleting a user: the future service physically removes all their
      ``user_roles``, ``user_permission_overrides`` and ``tenant_moderators`` rows, so
      ``restore`` does not bring the rights back. The resolver must still ignore users
      that are not ``is_active AND NOT is_deleted``.
    * Logs (``user_activity.telegram_user_id``, ``audit_logs.actor_id``) reference the
      Telegram id without a FK, so they survive the deletion of a user.
    * A physical ``DELETE`` cascades to ``user_roles``, ``user_permission_overrides`` and
      ``tenant_moderators``; ``granted_by_id`` of rows granted by the user becomes NULL.
    """

    __tablename__ = "users"
    __table_args__ = (CheckConstraint("telegram_user_id > 0", name="telegram_user_id_positive"),)

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    telegram_user_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str | None] = mapped_column(IanaTimezone(), nullable=True)
    is_superuser: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    @validates("timezone")
    def _validate_timezone(self, _key: str, value: str | None) -> str | None:
        return None if value is None else validate_timezone(value)


class UserRole(BaseEntity, Base):
    """Role assigned to a user (no soft delete: revoking deletes the row).

    * ``user_id`` lookups use ``uq_user_roles_user_id_role_id`` (leading column).
    * ``ON DELETE RESTRICT`` on ``role_id`` forbids purging a role that is still assigned.
    * Self-assignment (``granted_by_id == user_id``) is not checked by the database; the
      service forbids it (granting requires ``roles.manage``).
    * The resolver counts a row only if the user and the role are both
      ``is_active AND NOT is_deleted``.
    """

    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("roles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class UserPermissionOverride(BaseEntity, Base):
    """Per-user allow (``is_granted = true``) / deny (``false``) of one permission.

    * ``tenant_id`` NULL = all shops, otherwise the override applies to that shop only.
    * At most one override per (user, permission, tenant) - NULL tenant included
      (``UNIQUE NULLS NOT DISTINCT``, PostgreSQL 15+).
    * Resolution (future permission service): deny is stronger than allow; only rows with
      ``expires_at IS NULL OR expires_at > now()`` count, the user and the permission must
      be ``is_active AND NOT is_deleted`` and a tenant override needs a live tenant
      (``NOT tenants.is_deleted``).
    * An expired override still occupies its unique slot: the service upserts (updates
      the existing row) instead of inserting a new one. ``expires_at`` not being in the
      past is validated by the service (a CHECK can not use ``now()``).
    * Self-granting (``granted_by_id == user_id``) is forbidden by the service, not the
      database; tenant-scoped overrides of ``GLOBAL_PERMISSIONS`` likewise.
    * ``is_granted`` has no default: allow/deny is always an explicit choice.
    """

    __tablename__ = "user_permission_overrides"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "permission_id", "tenant_id", postgresql_nulls_not_distinct=True
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    is_granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class TenantModerator(BaseEntity, Base):
    """Shop assigned to a moderator (a user may moderate several shops).

    A user can work with a shop if there is a row here *or* they hold
    ``tenants.view_all``. ``user_id`` lookups use ``uq_tenant_moderators_user_id_tenant_id``.
    """

    __tablename__ = "tenant_moderators"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
