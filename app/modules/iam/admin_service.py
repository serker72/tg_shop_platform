"""IAM write services (use cases): tenants, staff users, roles, role permissions, user
roles, permission overrides and shop moderators.

Every mutating method follows one pattern:

1. ``PermissionService.require(actor, slug, tenant_id)`` (Q12) - before any write;
2. business rules (pure helpers below, unit-tested) and the change itself;
3. an ``AuditLogger`` record in the SAME transaction (``before`` / ``after`` payload);
4. ``session.commit()`` (any error -> rollback, nothing is written);
5. strictly AFTER the commit - ``invalidate(telegram_user_id)`` or ``invalidate_all``.

A request that changes nothing (the requested state already holds) commits the read-only
transaction and writes neither audit nor invalidation.

**Cache contract.** After every mutating call :attr:`_AdminService.cache_invalidated` tells
whether Redis confirmed the invalidation. ``False`` -> the change IS stored, but other
processes may serve the old permissions until the snapshot TTL; the failure is logged
(ERROR) and counted in ``iam_cache_invalidation_failures_total``. Callers (panel dialogs)
show a warning. There is no immediate retry: the cache circuit breaker makes it useless.

**Session contract.** The services commit the request session themselves, so it must not
hold foreign pending changes on entry (``session_not_clean``). Changes already *flushed*
by the caller can not be detected - do not mix them with these services in one session.

``is_superuser`` is never set here (CLI only). Actors and targets are identified by the
Telegram user id; a missing/inactive actor is answered with ``permission_denied`` (its
existence is not disclosed). Error codes are the module constants (``DomainError.code``).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Collection, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam.admin_repository import IamAdminRepository
from app.modules.iam.metrics import (
    CacheInvalidationFailures,
    iam_cache_invalidation_failures_total,
)
from app.modules.iam.models import (
    PERMISSION_SLUG_PATTERN,
    ROLE_SLUG_PATTERN,
    TENANT_SLUG_MAX_LENGTH,
    TENANT_SLUG_PATTERN,
    Role,
    Tenant,
    TenantModerator,
    TenantSettings,
    User,
    UserPermissionOverride,
    UserRole,
)
from app.modules.iam.permissions import (
    GLOBAL_PERMISSIONS,
    MODERATORS_MANAGE,
    ROLES_MANAGE,
    TENANTS_MANAGE,
    TENANTS_VIEW_ALL,
    USERS_MANAGE,
)
from app.modules.iam.service import PERMISSION_DENIED, PermissionService
from app.modules.logging.audit import SqlAuditLogger
from app.modules.logging.events import ActorType, AuditEvent
from app.modules.logging.ports import AuditLogger
from app.shared_kernel.domain.exceptions import (
    DomainError,
    EntityAlreadyExistsError,
    EntityNotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.shared_kernel.infrastructure.base_repository import BaseRepository

__all__ = [
    "CONFLICT",
    "EXPIRES_AT_NOT_IN_FUTURE",
    "GLOBAL_PERMISSION_TENANT_OVERRIDE",
    "INVALID_EXPIRES_AT",
    "INVALID_NAME",
    "INVALID_ROLE_SLUG",
    "INVALID_SETTINGS",
    "INVALID_TELEGRAM_USER_ID",
    "INVALID_TENANT_SLUG",
    "LANGUAGE_NOT_FOUND",
    "MODERATOR_ALREADY_ASSIGNED",
    "MODERATOR_HAS_GLOBAL_ACCESS",
    "MODERATOR_NOT_FOUND",
    "OVERRIDE_NOT_FOUND",
    "PERMISSION_NOT_FOUND",
    "PRIVILEGE_ESCALATION",
    "ROLE_ALREADY_ASSIGNED",
    "ROLE_NOT_ASSIGNED",
    "ROLE_NOT_FOUND",
    "ROLE_SLUG_TAKEN",
    "SELF_MODIFICATION_FORBIDDEN",
    "SESSION_NOT_CLEAN",
    "SUPERUSER_PROTECTED",
    "SYSTEM_ROLE_PROTECTED",
    "TENANT_NOT_FOUND",
    "TENANT_SETTINGS_FIELDS",
    "TENANT_SLUG_TAKEN",
    "USER_DELETED",
    "USER_EXISTS",
    "USER_NOT_FOUND",
    "AccessAdminService",
    "RoleAdminService",
    "TenantAdminService",
    "UserAdminService",
    "ensure_not_self",
    "ensure_superuser_protected",
    "ensure_system_role_mutable",
    "map_integrity_error",
    "normalize_username",
    "validate_expires_at",
    "validate_name",
    "validate_override_scope",
    "validate_permission_slugs",
    "validate_role_slug",
    "validate_telegram_user_id",
    "validate_tenant_settings",
    "validate_tenant_slug",
]

# --- error codes ---------------------------------------------------------------------
CONFLICT: Final = "conflict"
EXPIRES_AT_NOT_IN_FUTURE: Final = "expires_at_not_in_future"
GLOBAL_PERMISSION_TENANT_OVERRIDE: Final = "global_permission_tenant_override"
MODERATOR_HAS_GLOBAL_ACCESS: Final = "moderator_has_global_access"
INVALID_EXPIRES_AT: Final = "invalid_expires_at"
INVALID_NAME: Final = "invalid_name"
INVALID_ROLE_SLUG: Final = "invalid_role_slug"
INVALID_SETTINGS: Final = "invalid_settings"
INVALID_TELEGRAM_USER_ID: Final = "invalid_telegram_user_id"
INVALID_TENANT_SLUG: Final = "invalid_tenant_slug"
LANGUAGE_NOT_FOUND: Final = "language_not_found"
MODERATOR_ALREADY_ASSIGNED: Final = "moderator_already_assigned"
MODERATOR_NOT_FOUND: Final = "moderator_not_found"
OVERRIDE_NOT_FOUND: Final = "override_not_found"
PERMISSION_NOT_FOUND: Final = "permission_not_found"
PRIVILEGE_ESCALATION: Final = "privilege_escalation"
ROLE_ALREADY_ASSIGNED: Final = "role_already_assigned"
ROLE_NOT_ASSIGNED: Final = "role_not_assigned"
ROLE_NOT_FOUND: Final = "role_not_found"
ROLE_SLUG_TAKEN: Final = "role_slug_taken"
SELF_MODIFICATION_FORBIDDEN: Final = "self_modification_forbidden"
SESSION_NOT_CLEAN: Final = "session_not_clean"
SUPERUSER_PROTECTED: Final = "superuser_protected"
SYSTEM_ROLE_PROTECTED: Final = "system_role_protected"
TENANT_NOT_FOUND: Final = "tenant_not_found"
TENANT_SLUG_TAKEN: Final = "tenant_slug_taken"
USER_DELETED: Final = "user_deleted"
USER_EXISTS: Final = "user_exists"
USER_NOT_FOUND: Final = "user_not_found"

#: Editable ``TenantSettings`` fields and their kinds (``faq``/``extra`` are not in scope).
TENANT_SETTINGS_FIELDS: Final[Mapping[str, str]] = {
    "is_store_open": "bool",
    "is_buy_enabled": "bool",
    "is_refill_enabled": "bool",
    "is_referral_enabled": "bool",
    "is_notifications_enabled": "bool",
    "default_language_code": "language",
    "support_chat_id": "chat",
    "news_chat_id": "chat",
}

#: Unique constraint -> domain code (names from the IAM revisions' naming convention).
_UNIQUE_CODES: Final[Mapping[str, str]] = {
    "uq_tenants_slug": TENANT_SLUG_TAKEN,
    "uq_roles_slug": ROLE_SLUG_TAKEN,
    "uq_users_telegram_user_id": USER_EXISTS,
    "uq_user_roles_user_id_role_id": ROLE_ALREADY_ASSIGNED,
    "uq_tenant_moderators_user_id_tenant_id": MODERATOR_ALREADY_ASSIGNED,
}
#: FK names end with ``_<referred table>`` (``fk_user_roles_role_id_roles``).
_FK_CODES: Final[Mapping[str, str]] = {
    "users": USER_NOT_FOUND,
    "tenants": TENANT_NOT_FOUND,
    "roles": ROLE_NOT_FOUND,
    "permissions": PERMISSION_NOT_FOUND,
    "languages": LANGUAGE_NOT_FOUND,
}

_TENANT_SLUG_RE = re.compile(TENANT_SLUG_PATTERN)
_ROLE_SLUG_RE = re.compile(ROLE_SLUG_PATTERN)
_PERMISSION_SLUG_RE = re.compile(PERMISSION_SLUG_PATTERN)
_ACTOR_TYPE_USER: Final[ActorType] = "user"
_log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: Telegram ids (users, chats) are stored in PostgreSQL ``bigint``.
_BIGINT_MAX: Final = 2**63


class _NoChange(Exception):  # noqa: N818 - internal control-flow signal
    """The requested state already holds: commit the read, skip audit and invalidation."""


# --- pure business rules (unit-tested) -------------------------------------------------


def validate_name(name: str) -> str:
    """Trimmed non-empty display name."""
    value = name.strip() if isinstance(name, str) else ""
    if not value:
        raise ValidationError("Name must not be empty", code=INVALID_NAME)
    return value


def validate_tenant_slug(slug: str) -> str:
    if len(slug) > TENANT_SLUG_MAX_LENGTH or not _TENANT_SLUG_RE.fullmatch(slug):
        raise ValidationError(f"Malformed tenant slug: {slug!r}", code=INVALID_TENANT_SLUG)
    return slug


def validate_role_slug(slug: str) -> str:
    if not _ROLE_SLUG_RE.fullmatch(slug):
        raise ValidationError(f"Malformed role slug: {slug!r}", code=INVALID_ROLE_SLUG)
    return slug


def validate_telegram_user_id(telegram_user_id: int) -> int:
    if isinstance(telegram_user_id, bool) or not isinstance(telegram_user_id, int):
        raise ValidationError("telegram_user_id must be int", code=INVALID_TELEGRAM_USER_ID)
    if not 0 < telegram_user_id < _BIGINT_MAX:
        raise ValidationError(
            "telegram_user_id must be a positive bigint", code=INVALID_TELEGRAM_USER_ID
        )
    return telegram_user_id


def normalize_username(username: str | None) -> str | None:
    """Telegram username without ``@``; empty -> ``None``."""
    if username is None:
        return None
    value = username.strip().lstrip("@").strip()
    return value or None


def validate_permission_slugs(slugs: Iterable[str]) -> frozenset[str]:
    result = frozenset(slugs)
    bad = sorted(s for s in result if not _PERMISSION_SLUG_RE.fullmatch(s))
    if bad:
        raise ValidationError(f"Malformed permission slugs: {bad}", code=PERMISSION_NOT_FOUND)
    return result


def ensure_not_self(actor: User, target: User) -> None:
    """Nobody changes their own rights/status (self-grant, self-revoke, self-delete)."""
    if actor.id == target.id:
        raise PermissionDeniedError(
            "Changing your own access is forbidden", code=SELF_MODIFICATION_FORBIDDEN
        )


def ensure_superuser_protected(actor: User, target: User) -> None:
    """Only a superuser may change another superuser."""
    if target.is_superuser and not actor.is_superuser:
        raise PermissionDeniedError("Target is a superuser", code=SUPERUSER_PROTECTED)


def ensure_system_role_mutable(role: Role, action: str) -> None:
    """System roles: slug, activity, deletion and permission set are frozen."""
    if role.is_system:
        raise ValidationError(
            f"System role {role.slug!r} can not be changed ({action})", code=SYSTEM_ROLE_PROTECTED
        )


def validate_override_scope(slug: str, tenant_id: UUID | None) -> None:
    if tenant_id is not None and slug in GLOBAL_PERMISSIONS:
        raise ValidationError(
            f"{slug} is a global permission: tenant override is forbidden",
            code=GLOBAL_PERMISSION_TENANT_OVERRIDE,
        )


def validate_expires_at(expires_at: datetime | None, now: datetime) -> None:
    if expires_at is None:
        return
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValidationError("expires_at must be timezone-aware", code=INVALID_EXPIRES_AT)
    if expires_at <= now:
        raise ValidationError("expires_at must be in the future", code=EXPIRES_AT_NOT_IN_FUTURE)


def validate_tenant_settings(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Known fields with the right types; language existence is checked by the service."""
    unknown = sorted(set(changes) - set(TENANT_SETTINGS_FIELDS))
    if unknown:
        raise ValidationError(f"Unknown settings: {unknown}", code=INVALID_SETTINGS)
    result: dict[str, Any] = {}
    for key, value in changes.items():
        kind = TENANT_SETTINGS_FIELDS[key]
        if kind == "bool":
            ok = isinstance(value, bool)
        elif kind == "chat":
            ok = value is None or (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value != 0
                and -_BIGINT_MAX < value < _BIGINT_MAX
            )
        else:
            ok = value is None or (isinstance(value, str) and bool(value))
        if not ok:
            raise ValidationError(f"Invalid value for {key}", code=INVALID_SETTINGS)
        result[key] = value
    return result


def map_integrity_error(error: IntegrityError) -> DomainError:
    """Database constraint error -> domain error; the SQL text is never put in the message."""
    orig = error.orig
    sqlstate = getattr(orig, "sqlstate", None)
    constraint = getattr(getattr(orig, "diag", None), "constraint_name", None) or ""
    if sqlstate == "23505":
        return EntityAlreadyExistsError(
            "Entity already exists", code=_UNIQUE_CODES.get(constraint, CONFLICT)
        )
    if sqlstate == "23503":
        for table, code in _FK_CODES.items():
            if constraint.endswith(f"_{table}"):
                return EntityNotFoundError("Referenced entity not found", code=code)
    return EntityAlreadyExistsError("Concurrent change conflict", code=CONFLICT)


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


# --- base -----------------------------------------------------------------------------


class _AdminService:
    """Shared plumbing: authorization, audit, transaction, invalidation after commit.

    An instance is NOT for concurrent use: it is bound to one request session (REQUEST
    scope) and keeps per-call state (:attr:`cache_invalidated`); run calls sequentially.
    """

    def __init__(
        self,
        session: AsyncSession,
        permissions: PermissionService,
        *,
        audit: AuditLogger | None = None,
        repository: IamAdminRepository | None = None,
        clock: Callable[[], datetime] = _utcnow,
        invalidation_failures: CacheInvalidationFailures = iam_cache_invalidation_failures_total,
    ) -> None:
        self._session = session
        self._permissions = permissions
        self._audit_logger: AuditLogger = audit or SqlAuditLogger()
        self._repo = repository or IamAdminRepository(session)
        self._clock = clock
        self._failures = invalidation_failures
        self._noop = False
        #: Result of the last mutating call: ``False`` -> Redis did not confirm the
        #: invalidation (change stored, stale permissions possible until the TTL).
        self.cache_invalidated = True

    async def _authorize(
        self, actor_telegram_id: int, slug: str, tenant_id: UUID | None = None
    ) -> User:
        """``require`` + the live actor row (needed for ``granted_by_id`` / self checks)."""
        await self._permissions.require(actor_telegram_id, slug, tenant_id)
        actor = await self._repo.get_user_by_telegram_id(actor_telegram_id)
        if actor is None or not actor.is_active:
            raise PermissionDeniedError(f"Permission denied: {slug}", code=PERMISSION_DENIED)
        return actor

    async def _ensure_actor_holds(
        self, actor_telegram_id: int, slugs: Iterable[str], tenant_id: UUID | None = None
    ) -> None:
        """Anti-escalation: nobody hands out a permission they do not hold themselves."""
        missing = []
        for slug in sorted(set(slugs)):
            if not (await self._permissions.check(actor_telegram_id, slug, tenant_id)).allowed:
                missing.append(slug)
        if missing:
            raise PermissionDeniedError(
                f"Can not grant permissions you do not hold: {missing}", code=PRIVILEGE_ESCALATION
            )

    async def _target_user(self, telegram_user_id: int, *, include_deleted: bool = False) -> User:
        user = await self._repo.get_user_by_telegram_id(
            telegram_user_id, include_deleted=include_deleted, for_update=True
        )
        if user is None:
            raise EntityNotFoundError("User not found", code=USER_NOT_FOUND)
        return user

    async def _live_tenant(self, tenant_id: UUID, *, for_update: bool = False) -> Tenant:
        tenant = await self._repo.get_tenant(tenant_id, for_update=for_update)
        if tenant is None:
            raise EntityNotFoundError("Tenant not found", code=TENANT_NOT_FOUND)
        return tenant

    async def _audit(
        self,
        actor_telegram_id: int,
        action: str,
        *,
        entity_type: str,
        entity_id: UUID | None,
        tenant_id: UUID | None = None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
    ) -> None:
        await self._audit_logger.record(
            self._session,
            AuditEvent(
                actor_type=_ACTOR_TYPE_USER,
                action=action,
                tenant_id=tenant_id,
                actor_id=actor_telegram_id,
                entity_type=entity_type,
                entity_id=entity_id,
                payload={"before": _jsonable(before), "after": _jsonable(after)},
                result="success",
            ),
        )

    def _ensure_clean_session(self) -> None:
        for name in ("new", "dirty", "deleted"):
            if getattr(self._session, name, None):
                raise ValidationError(
                    "Session holds foreign pending changes", code=SESSION_NOT_CLEAN
                )

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        """Commit on success; :class:`_NoChange` -> commit the read only; error -> rollback."""
        self._ensure_clean_session()
        self._noop = False
        self.cache_invalidated = True
        try:
            yield
            await self._session.commit()
        except _NoChange:
            # Nothing pending: commit ends the read transaction (and its row locks) without
            # expiring the loaded objects (``rollback`` would).
            await self._session.commit()
            self._noop = True
        except IntegrityError as error:
            await self._session.rollback()
            raise map_integrity_error(error) from error
        except BaseException:
            await self._session.rollback()
            raise

    async def _invalidate(self, users: Iterable[int] = (), *, everything: bool = False) -> bool:
        """AFTER commit only; sets and returns :attr:`cache_invalidated` (no retry)."""
        if self._noop:
            return True
        targets = tuple(users)
        ok = True
        if everything:
            if not (await self._permissions.invalidate_all()).ok:
                ok = False
                self._failures.inc(scope="all")
        else:
            for telegram_user_id in targets:
                if not (await self._permissions.invalidate(telegram_user_id)).ok:
                    ok = False
                    self._failures.inc(scope="user")
        if not ok:
            _log.error(
                "iam_admin_cache_invalidation_failed",
                telegram_user_ids=list(targets),
                everything=everything,
            )
        self.cache_invalidated = ok
        return ok


def _tenant_state(tenant: Tenant) -> dict[str, Any]:
    return {
        "name": tenant.name,
        "slug": tenant.slug,
        "is_active": tenant.is_active,
        "is_deleted": tenant.is_deleted,
    }


def _user_state(user: User) -> dict[str, Any]:
    return {
        "telegram_user_id": user.telegram_user_id,
        "telegram_user_name": user.telegram_user_name,
        "is_active": user.is_active,
        "is_deleted": user.is_deleted,
    }


def _role_state(role: Role) -> dict[str, Any]:
    return {
        "name": role.name,
        "slug": role.slug,
        "is_active": role.is_active,
        "is_deleted": role.is_deleted,
        "is_system": role.is_system,
    }


def _override_state(row: UserPermissionOverride, slug: str) -> dict[str, Any]:
    return {
        "permission": slug,
        "tenant_id": row.tenant_id,
        "is_granted": row.is_granted,
        "expires_at": row.expires_at,
        "reason": row.reason,
    }


# --- tenants --------------------------------------------------------------------------


class TenantAdminService(_AdminService):
    """Shops and their settings; every mutation requires ``tenants.manage``."""

    async def create(self, actor_telegram_id: int, *, name: str, slug: str) -> Tenant:
        """Create a tenant together with its ``TenantSettings`` (one transaction)."""
        async with self._transaction():
            await self._authorize(actor_telegram_id, TENANTS_MANAGE)
            name = validate_name(name)
            validate_tenant_slug(slug)
            if await self._repo.tenant_slug_taken(slug):
                raise EntityAlreadyExistsError("Tenant slug is taken", code=TENANT_SLUG_TAKEN)
            tenant = Tenant(name=name, slug=slug)
            self._session.add(tenant)
            await self._session.flush()
            self._session.add(TenantSettings(tenant_id=tenant.id))
            await self._session.flush()
            await self._audit(
                actor_telegram_id,
                "iam.tenant.create",
                entity_type="tenant",
                entity_id=tenant.id,
                tenant_id=tenant.id,
                after=_tenant_state(tenant),
            )
        return tenant

    async def update(self, actor_telegram_id: int, tenant_id: UUID, *, name: str) -> Tenant:
        """Rename a live tenant (the slug is immutable: it is never reused)."""
        async with self._transaction():
            await self._authorize(actor_telegram_id, TENANTS_MANAGE)
            tenant = await self._live_tenant(tenant_id, for_update=True)
            new_name = validate_name(name)
            if tenant.name == new_name:
                raise _NoChange
            before = _tenant_state(tenant)
            tenant.name = new_name
            await self._session.flush()
            await self._audit(
                actor_telegram_id,
                "iam.tenant.update",
                entity_type="tenant",
                entity_id=tenant.id,
                tenant_id=tenant.id,
                before=before,
                after=_tenant_state(tenant),
            )
        return tenant

    async def activate(self, actor_telegram_id: int, tenant_id: UUID) -> Tenant:
        return await self._set_active(actor_telegram_id, tenant_id, active=True)

    async def deactivate(self, actor_telegram_id: int, tenant_id: UUID) -> Tenant:
        return await self._set_active(actor_telegram_id, tenant_id, active=False)

    async def _set_active(self, actor_telegram_id: int, tenant_id: UUID, *, active: bool) -> Tenant:
        async with self._transaction():
            await self._authorize(actor_telegram_id, TENANTS_MANAGE)
            tenant = await self._live_tenant(tenant_id, for_update=True)
            if tenant.is_active == active:
                raise _NoChange
            before = _tenant_state(tenant)
            tenant.is_active = active
            await self._session.flush()
            await self._audit(
                actor_telegram_id,
                "iam.tenant.activate" if active else "iam.tenant.deactivate",
                entity_type="tenant",
                entity_id=tenant.id,
                tenant_id=tenant.id,
                before=before,
                after=_tenant_state(tenant),
            )
        await self._invalidate(everything=True)
        return tenant

    async def soft_delete(self, actor_telegram_id: int, tenant_id: UUID) -> None:
        async with self._transaction():
            await self._authorize(actor_telegram_id, TENANTS_MANAGE)
            tenant = await self._live_tenant(tenant_id, for_update=True)
            before = _tenant_state(tenant)
            await BaseRepository(self._session, Tenant).soft_delete(tenant)
            await self._audit(
                actor_telegram_id,
                "iam.tenant.delete",
                entity_type="tenant",
                entity_id=tenant.id,
                tenant_id=tenant.id,
                before=before,
                after=_tenant_state(tenant),
            )
        await self._invalidate(everything=True)

    async def restore(self, actor_telegram_id: int, tenant_id: UUID) -> Tenant:
        async with self._transaction():
            await self._authorize(actor_telegram_id, TENANTS_MANAGE)
            tenant = await self._repo.get_tenant(tenant_id, include_deleted=True, for_update=True)
            if tenant is None:
                raise EntityNotFoundError("Tenant not found", code=TENANT_NOT_FOUND)
            if not tenant.is_deleted:
                raise _NoChange
            before = _tenant_state(tenant)
            await BaseRepository(self._session, Tenant).restore(tenant)
            await self._audit(
                actor_telegram_id,
                "iam.tenant.restore",
                entity_type="tenant",
                entity_id=tenant.id,
                tenant_id=tenant.id,
                before=before,
                after=_tenant_state(tenant),
            )
        await self._invalidate(everything=True)
        return tenant

    async def update_settings(
        self, actor_telegram_id: int, tenant_id: UUID, changes: Mapping[str, Any]
    ) -> TenantSettings:
        """Flags, ``default_language_code`` (live language or ``None``) and chat ids."""
        async with self._transaction():
            await self._authorize(actor_telegram_id, TENANTS_MANAGE)
            values = validate_tenant_settings(changes)
            await self._live_tenant(tenant_id)
            settings = await self._repo.get_tenant_settings(tenant_id, for_update=True)
            if settings is None:
                raise EntityNotFoundError("Tenant settings not found", code=TENANT_NOT_FOUND)
            values = {k: v for k, v in values.items() if getattr(settings, k) != v}
            if not values:
                raise _NoChange
            code = values.get("default_language_code")
            if code is not None and not await self._repo.language_is_live(code):
                raise ValidationError(f"Unknown language {code!r}", code=LANGUAGE_NOT_FOUND)
            before = {key: getattr(settings, key) for key in values}
            for key, value in values.items():
                setattr(settings, key, value)
            await self._session.flush()
            await self._audit(
                actor_telegram_id,
                "iam.tenant.settings_update",
                entity_type="tenant_settings",
                entity_id=settings.id,
                tenant_id=tenant_id,
                before=before,
                after=values,
            )
        return settings

    async def list_tenants(self, actor_telegram_id: int) -> list[Tenant]:
        """Live tenants visible to the actor: all with ``tenants.view_all``, else assigned."""
        snapshot = await self._permissions.get_snapshot(actor_telegram_id)
        if snapshot.decide(TENANTS_VIEW_ALL).allowed:
            return await self._repo.list_tenants()
        return await self._repo.list_tenants(snapshot.tenants)


# --- users ----------------------------------------------------------------------------


class UserAdminService(_AdminService):
    """Staff users (``users`` holds staff only; shop customers have no permissions).

    Adding, (de)activation, deletion and restore need the global ``users.manage``.
    """

    async def add_by_telegram_id(
        self, actor_telegram_id: int, telegram_user_id: int, username: str | None = None
    ) -> User:
        """New staff user without rights (Q3); ``is_superuser`` stays ``False``."""
        async with self._transaction():
            await self._authorize(actor_telegram_id, USERS_MANAGE)
            validate_telegram_user_id(telegram_user_id)
            existing = await self._repo.get_user_by_telegram_id(
                telegram_user_id, include_deleted=True
            )
            if existing is not None:
                if existing.is_deleted:
                    raise EntityAlreadyExistsError(
                        "User is deleted: use restore", code=USER_DELETED
                    )
                raise EntityAlreadyExistsError("User already exists", code=USER_EXISTS)
            user = User(
                telegram_user_id=telegram_user_id,
                telegram_user_name=normalize_username(username),
                is_superuser=False,
            )
            self._session.add(user)
            await self._session.flush()
            await self._audit(
                actor_telegram_id,
                "iam.user.create",
                entity_type="user",
                entity_id=user.id,
                after=_user_state(user),
            )
        await self._invalidate([telegram_user_id])  # drop the negative cache entry
        return user

    async def update_username(self, telegram_user_id: int, username: str | None) -> User | None:
        """Pick up the username on ``/start`` (self-reported data, no permission check).

        Unknown or deleted user -> ``None``; unchanged -> plain read, no lock/commit/audit.
        """
        new = normalize_username(username)
        current = await self._repo.get_user_by_telegram_id(telegram_user_id)
        if current is None or current.telegram_user_name == new:
            return current
        async with self._transaction():
            user = await self._repo.get_user_by_telegram_id(telegram_user_id, for_update=True)
            if user is None or user.telegram_user_name == new:
                raise _NoChange
            before = _user_state(user)
            user.telegram_user_name = new
            await self._session.flush()
            await self._audit(
                telegram_user_id,
                "iam.user.update_username",
                entity_type="user",
                entity_id=user.id,
                before=before,
                after=_user_state(user),
            )
        return user

    async def activate(self, actor_telegram_id: int, telegram_user_id: int) -> User:
        return await self._set_active(actor_telegram_id, telegram_user_id, active=True)

    async def deactivate(self, actor_telegram_id: int, telegram_user_id: int) -> User:
        return await self._set_active(actor_telegram_id, telegram_user_id, active=False)

    async def _set_active(
        self, actor_telegram_id: int, telegram_user_id: int, *, active: bool
    ) -> User:
        async with self._transaction():
            actor = await self._authorize(actor_telegram_id, USERS_MANAGE)
            user = await self._target_user(telegram_user_id)
            ensure_not_self(actor, user)
            ensure_superuser_protected(actor, user)
            if user.is_active == active:
                raise _NoChange
            if active:
                # Activation gives the rights back: the actor must hold every one of them.
                grants = await self._repo.user_granted_permissions(user.id, now=self._clock())
                by_tenant: dict[UUID | None, set[str]] = defaultdict(set)
                for slug, tenant_id in grants:
                    by_tenant[tenant_id].add(slug)
                for tenant_id, slugs in by_tenant.items():
                    await self._ensure_actor_holds(actor_telegram_id, slugs, tenant_id)
            before = _user_state(user)
            user.is_active = active
            await self._session.flush()
            await self._audit(
                actor_telegram_id,
                "iam.user.activate" if active else "iam.user.deactivate",
                entity_type="user",
                entity_id=user.id,
                before=before,
                after=_user_state(user),
            )
        await self._invalidate([telegram_user_id])
        return user

    async def soft_delete(self, actor_telegram_id: int, telegram_user_id: int) -> None:
        """Soft-delete and physically remove roles, overrides and shop assignments."""
        async with self._transaction():
            actor = await self._authorize(actor_telegram_id, USERS_MANAGE)
            user = await self._target_user(telegram_user_id)
            ensure_not_self(actor, user)
            ensure_superuser_protected(actor, user)
            before = _user_state(user)
            removed = await self._repo.delete_user_assignments(user.id)
            await BaseRepository(self._session, User).soft_delete(user)
            await self._audit(
                actor_telegram_id,
                "iam.user.delete",
                entity_type="user",
                entity_id=user.id,
                before=before,
                after={**_user_state(user), "removed": removed},
            )
        await self._invalidate([telegram_user_id])

    async def restore(self, actor_telegram_id: int, telegram_user_id: int) -> User:
        """Bring the row back; rights are NOT restored (they were removed on delete)."""
        async with self._transaction():
            actor = await self._authorize(actor_telegram_id, USERS_MANAGE)
            user = await self._target_user(telegram_user_id, include_deleted=True)
            ensure_not_self(actor, user)
            ensure_superuser_protected(actor, user)
            if not user.is_deleted:
                raise _NoChange
            before = _user_state(user)
            await BaseRepository(self._session, User).restore(user)
            await self._audit(
                actor_telegram_id,
                "iam.user.restore",
                entity_type="user",
                entity_id=user.id,
                before=before,
                after=_user_state(user),
            )
        await self._invalidate([telegram_user_id])
        return user


# --- roles ----------------------------------------------------------------------------


class RoleAdminService(_AdminService):
    """Custom roles and their permissions; everything requires ``roles.manage``."""

    async def _role(self, role_id: UUID) -> Role:
        role = await self._repo.get_role(role_id, for_update=True)
        if role is None:
            raise EntityNotFoundError("Role not found", code=ROLE_NOT_FOUND)
        return role

    async def _resolve_permissions(self, slugs: Collection[str]) -> dict[str, UUID]:
        wanted = validate_permission_slugs(slugs)
        found = await self._repo.live_permissions(wanted)
        missing = sorted(wanted - found.keys())
        if missing:
            raise ValidationError(f"Unknown permissions: {missing}", code=PERMISSION_NOT_FOUND)
        return {slug: p.id for slug, p in found.items()}

    async def create(
        self,
        actor_telegram_id: int,
        *,
        name: str,
        slug: str,
        permission_slugs: Collection[str] = (),
    ) -> Role:
        async with self._transaction():
            await self._authorize(actor_telegram_id, ROLES_MANAGE)
            name = validate_name(name)
            validate_role_slug(slug)
            permissions = await self._resolve_permissions(permission_slugs)
            await self._ensure_actor_holds(actor_telegram_id, permissions)
            if await self._repo.role_slug_taken(slug):
                raise EntityAlreadyExistsError("Role slug is taken", code=ROLE_SLUG_TAKEN)
            role = Role(name=name, slug=slug, is_system=False)
            self._session.add(role)
            await self._session.flush()
            await self._repo.replace_role_permissions(role.id, permissions.values())
            await self._audit(
                actor_telegram_id,
                "iam.role.create",
                entity_type="role",
                entity_id=role.id,
                after={**_role_state(role), "permissions": set(permissions)},
            )
        return role

    async def rename(self, actor_telegram_id: int, role_id: UUID, name: str) -> Role:
        """Display name only; allowed for system roles too."""
        async with self._transaction():
            await self._authorize(actor_telegram_id, ROLES_MANAGE)
            role = await self._role(role_id)
            new_name = validate_name(name)
            if role.name == new_name:
                raise _NoChange
            before = _role_state(role)
            role.name = new_name
            await self._session.flush()
            await self._audit(
                actor_telegram_id,
                "iam.role.rename",
                entity_type="role",
                entity_id=role.id,
                before=before,
                after=_role_state(role),
            )
        return role

    async def change_slug(self, actor_telegram_id: int, role_id: UUID, slug: str) -> Role:
        async with self._transaction():
            await self._authorize(actor_telegram_id, ROLES_MANAGE)
            role = await self._role(role_id)
            ensure_system_role_mutable(role, "change slug")
            validate_role_slug(slug)
            if slug == role.slug:
                raise _NoChange
            if await self._repo.role_slug_taken(slug):
                raise EntityAlreadyExistsError("Role slug is taken", code=ROLE_SLUG_TAKEN)
            before = _role_state(role)
            role.slug = slug
            await self._session.flush()
            await self._audit(
                actor_telegram_id,
                "iam.role.change_slug",
                entity_type="role",
                entity_id=role.id,
                before=before,
                after=_role_state(role),
            )
        await self._invalidate(everything=True)  # snapshots carry role slugs
        return role

    async def activate(self, actor_telegram_id: int, role_id: UUID) -> Role:
        return await self._set_active(actor_telegram_id, role_id, active=True)

    async def deactivate(self, actor_telegram_id: int, role_id: UUID) -> Role:
        return await self._set_active(actor_telegram_id, role_id, active=False)

    async def _set_active(self, actor_telegram_id: int, role_id: UUID, *, active: bool) -> Role:
        async with self._transaction():
            await self._authorize(actor_telegram_id, ROLES_MANAGE)
            role = await self._role(role_id)
            ensure_system_role_mutable(role, "activate" if active else "deactivate")
            if role.is_active == active:
                raise _NoChange
            if active:
                await self._ensure_actor_holds(
                    actor_telegram_id,
                    await self._repo.role_permission_slugs(role.id, live_only=True),
                )
            before = _role_state(role)
            role.is_active = active
            await self._session.flush()
            await self._audit(
                actor_telegram_id,
                "iam.role.activate" if active else "iam.role.deactivate",
                entity_type="role",
                entity_id=role.id,
                before=before,
                after=_role_state(role),
            )
        await self._invalidate(everything=True)
        return role

    async def soft_delete(self, actor_telegram_id: int, role_id: UUID) -> None:
        async with self._transaction():
            await self._authorize(actor_telegram_id, ROLES_MANAGE)
            role = await self._role(role_id)
            ensure_system_role_mutable(role, "delete")
            before = _role_state(role)
            await BaseRepository(self._session, Role).soft_delete(role)
            await self._audit(
                actor_telegram_id,
                "iam.role.delete",
                entity_type="role",
                entity_id=role.id,
                before=before,
                after=_role_state(role),
            )
        await self._invalidate(everything=True)

    async def set_permissions(
        self, actor_telegram_id: int, role_id: UUID, permission_slugs: Collection[str]
    ) -> frozenset[str]:
        """Replace the permission set of a custom role (system roles are frozen)."""
        async with self._transaction():
            await self._authorize(actor_telegram_id, ROLES_MANAGE)
            role = await self._role(role_id)
            ensure_system_role_mutable(role, "set permissions")
            permissions = await self._resolve_permissions(permission_slugs)
            before = await self._repo.role_permission_slugs(role.id)
            after = frozenset(permissions)
            if before == after:
                raise _NoChange
            await self._ensure_actor_holds(actor_telegram_id, after - before)
            await self._repo.replace_role_permissions(role.id, permissions.values())
            await self._audit(
                actor_telegram_id,
                "iam.role.set_permissions",
                entity_type="role",
                entity_id=role.id,
                before={"permissions": before},
                after={"permissions": after},
            )
        await self._invalidate(everything=True)
        return after


# --- assignments: user roles, overrides, shop moderators --------------------------------


class AccessAdminService(_AdminService):
    """User roles and overrides (``roles.manage``), shop moderators (``moderators.manage``)."""

    async def assign_role(
        self, actor_telegram_id: int, telegram_user_id: int, role_id: UUID
    ) -> UserRole:
        async with self._transaction():
            actor = await self._authorize(actor_telegram_id, ROLES_MANAGE)
            user = await self._target_user(telegram_user_id)
            ensure_not_self(actor, user)
            ensure_superuser_protected(actor, user)
            role = await self._repo.get_role(role_id)
            if role is None or not role.is_active:
                raise EntityNotFoundError("Role not found", code=ROLE_NOT_FOUND)
            await self._ensure_actor_holds(
                actor_telegram_id, await self._repo.role_permission_slugs(role.id, live_only=True)
            )
            if await self._repo.get_user_role(user.id, role.id) is not None:
                raise EntityAlreadyExistsError("Role already assigned", code=ROLE_ALREADY_ASSIGNED)
            granted = await self._repo.role_permission_slugs(role.id, live_only=True)
            link = UserRole(user_id=user.id, role_id=role.id, granted_by_id=actor.id)
            self._session.add(link)
            await self._session.flush()
            if TENANTS_VIEW_ALL in granted:
                await self._drop_assignments(actor_telegram_id, user)
            await self._audit(
                actor_telegram_id,
                "iam.user_role.assign",
                entity_type="user",
                entity_id=user.id,
                after={"telegram_user_id": telegram_user_id, "role": role.slug},
            )
        await self._invalidate([telegram_user_id])
        return link

    async def revoke_role(
        self, actor_telegram_id: int, telegram_user_id: int, role_id: UUID
    ) -> None:
        async with self._transaction():
            actor = await self._authorize(actor_telegram_id, ROLES_MANAGE)
            user = await self._target_user(telegram_user_id)
            ensure_not_self(actor, user)
            ensure_superuser_protected(actor, user)
            link = await self._repo.get_user_role(user.id, role_id)
            if link is None:
                raise EntityNotFoundError("Role is not assigned", code=ROLE_NOT_ASSIGNED)
            role = await self._repo.get_role(role_id, include_deleted=True)
            await self._session.delete(link)
            await self._session.flush()
            await self._audit(
                actor_telegram_id,
                "iam.user_role.revoke",
                entity_type="user",
                entity_id=user.id,
                before={
                    "telegram_user_id": telegram_user_id,
                    "role": None if role is None else role.slug,
                },
            )
        await self._invalidate([telegram_user_id])

    async def upsert_override(
        self,
        actor_telegram_id: int,
        telegram_user_id: int,
        permission_slug: str,
        *,
        is_granted: bool,
        tenant_id: UUID | None = None,
        expires_at: datetime | None = None,
        reason: str | None = None,
    ) -> UserPermissionOverride:
        """Allow/deny one permission in the (user, permission, tenant) slot.

        ``INSERT ... ON CONFLICT DO UPDATE``: two concurrent upserts of an empty slot both
        succeed (the last one wins) instead of failing on the unique constraint.
        """
        async with self._transaction():
            actor = await self._authorize(actor_telegram_id, ROLES_MANAGE)
            validate_override_scope(permission_slug, tenant_id)
            validate_expires_at(expires_at, self._clock())
            user = await self._target_user(telegram_user_id)
            ensure_not_self(actor, user)
            ensure_superuser_protected(actor, user)
            permission = (await self._repo.live_permissions([permission_slug])).get(permission_slug)
            if permission is None:
                raise ValidationError(
                    f"Unknown permission {permission_slug!r}", code=PERMISSION_NOT_FOUND
                )
            if tenant_id is not None:
                await self._live_tenant(tenant_id)
            if is_granted:
                await self._ensure_actor_holds(actor_telegram_id, [permission_slug], tenant_id)
            existing = await self._repo.get_override(user.id, permission.id, tenant_id)
            before = None if existing is None else _override_state(existing, permission_slug)
            if existing is not None and (
                existing.is_granted,
                existing.expires_at,
                existing.reason,
            ) == (is_granted, expires_at, reason):
                raise _NoChange
            row, inserted = await self._repo.upsert_override(
                user_id=user.id,
                permission_id=permission.id,
                tenant_id=tenant_id,
                is_granted=is_granted,
                expires_at=expires_at,
                reason=reason,
                granted_by_id=actor.id,
            )
            await self._audit(
                actor_telegram_id,
                "iam.override.upsert",
                entity_type="user",
                entity_id=user.id,
                tenant_id=tenant_id,
                before=before,
                after=(
                    # A concurrent upsert filled the empty slot first: ``before`` is unknown.
                    {**_override_state(row, permission_slug), "concurrent_update": True}
                    if existing is None and not inserted
                    else _override_state(row, permission_slug)
                ),
            )
            if is_granted and tenant_id is None and permission_slug == TENANTS_VIEW_ALL:
                await self._drop_assignments(actor_telegram_id, user)
        if self._noop and existing is not None:
            return existing
        await self._invalidate([telegram_user_id])
        return row

    async def remove_override(
        self,
        actor_telegram_id: int,
        telegram_user_id: int,
        permission_slug: str,
        *,
        tenant_id: UUID | None = None,
    ) -> None:
        """Delete the slot. Removing a deny of a live permission grants it back, so the
        actor must hold that permission (anti-escalation); inactive permissions are found too.
        """
        async with self._transaction():
            actor = await self._authorize(actor_telegram_id, ROLES_MANAGE)
            user = await self._target_user(telegram_user_id)
            ensure_not_self(actor, user)
            ensure_superuser_protected(actor, user)
            permission = await self._repo.get_permission_by_slug(permission_slug)
            row = (
                None
                if permission is None
                else await self._repo.get_override(user.id, permission.id, tenant_id)
            )
            if permission is None or row is None:
                raise EntityNotFoundError("Override not found", code=OVERRIDE_NOT_FOUND)
            if not row.is_granted and permission.is_active and not permission.is_deleted:
                await self._ensure_actor_holds(actor_telegram_id, [permission_slug], tenant_id)
            before = _override_state(row, permission_slug)
            await self._session.delete(row)
            await self._session.flush()
            await self._audit(
                actor_telegram_id,
                "iam.override.remove",
                entity_type="user",
                entity_id=user.id,
                tenant_id=tenant_id,
                before=before,
            )
        await self._invalidate([telegram_user_id])

    async def assign_moderator(
        self, actor_telegram_id: int, telegram_user_id: int, tenant_id: UUID
    ) -> TenantModerator:
        async with self._transaction():
            actor = await self._authorize(actor_telegram_id, MODERATORS_MANAGE, tenant_id)
            user = await self._target_user(telegram_user_id)
            ensure_not_self(actor, user)
            ensure_superuser_protected(actor, user)
            await self._live_tenant(tenant_id)
            await self._ensure_not_global(user)
            if await self._repo.get_tenant_moderator(user.id, tenant_id) is not None:
                raise EntityAlreadyExistsError(
                    "Moderator already assigned", code=MODERATOR_ALREADY_ASSIGNED
                )
            row = TenantModerator(user_id=user.id, tenant_id=tenant_id)
            self._session.add(row)
            await self._session.flush()
            await self._audit(
                actor_telegram_id,
                "iam.moderator.assign",
                entity_type="user",
                entity_id=user.id,
                tenant_id=tenant_id,
                after={"telegram_user_id": telegram_user_id, "tenant_id": tenant_id},
            )
        await self._invalidate([telegram_user_id])
        return row

    async def _ensure_not_global(self, user: User) -> None:
        """A superuser / holder of a global ``tenants.view_all`` sees every shop already."""
        if user.is_superuser or await self._repo.holds_global_view_all(user.id, now=self._clock()):
            raise ValidationError(
                "Staff user with global access is never assigned to shops",
                code=MODERATOR_HAS_GLOBAL_ACCESS,
            )

    async def _drop_assignments(self, actor_telegram_id: int, user: User) -> None:
        """Remove every shop assignment of ``user`` (it got a global ``tenants.view_all``)."""
        for row in await self._repo.tenant_moderators_of(user.id):
            await self._session.delete(row)
            await self._session.flush()
            await self._audit(
                actor_telegram_id,
                "iam.moderator.unassign",
                entity_type="user",
                entity_id=user.id,
                tenant_id=row.tenant_id,
                before={
                    "user_id": user.id,
                    "telegram_user_id": user.telegram_user_id,
                    "tenant_id": row.tenant_id,
                    "reason": "global_access",
                },
            )

    async def unassign_moderator(
        self, actor_telegram_id: int, telegram_user_id: int, tenant_id: UUID
    ) -> None:
        async with self._transaction():
            actor = await self._authorize(actor_telegram_id, MODERATORS_MANAGE, tenant_id)
            user = await self._target_user(telegram_user_id)
            ensure_not_self(actor, user)
            ensure_superuser_protected(actor, user)
            row = await self._repo.get_tenant_moderator(user.id, tenant_id)
            if row is None:
                raise EntityNotFoundError("Moderator not assigned", code=MODERATOR_NOT_FOUND)
            await self._session.delete(row)
            await self._session.flush()
            await self._audit(
                actor_telegram_id,
                "iam.moderator.unassign",
                entity_type="user",
                entity_id=user.id,
                tenant_id=tenant_id,
                before={"telegram_user_id": telegram_user_id, "tenant_id": tenant_id},
            )
        await self._invalidate([telegram_user_id])
