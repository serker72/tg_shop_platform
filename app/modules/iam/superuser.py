"""Superuser installer: grant/revoke ``users.is_superuser`` (CLI only, R-II.1).

Separate from :class:`~app.modules.iam.admin_service.UserAdminService` on purpose: the admin
services never set ``is_superuser`` and always authorize an actor, while the installer runs
from the server console without one (``actor_type="system"`` in ``audit_logs``). The
transaction discipline is the same:

1. the session must hold no foreign pending changes (``session_not_clean``);
2. rows are locked ``FOR UPDATE`` with ``populate_existing``;
3. the change and its ``AuditLog`` record commit together; nothing changed -> no audit;
4. strictly AFTER the commit - ``PermissionService.invalidate(telegram_user_id)``. Redis
   failure does not undo the change: :attr:`SuperuserResult.cache_invalidated` is ``False``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam.admin_repository import IamAdminRepository
from app.modules.iam.admin_service import (
    SESSION_NOT_CLEAN,
    USER_NOT_FOUND,
    map_integrity_error,
    normalize_username,
    validate_telegram_user_id,
)
from app.modules.iam.metrics import iam_cache_invalidation_failures_total
from app.modules.iam.models import Permission, User
from app.modules.iam.permissions import SEED_PERMISSION_SLUGS
from app.modules.iam.service import PermissionService
from app.modules.logging.audit import SqlAuditLogger
from app.modules.logging.events import AuditEvent
from app.modules.logging.ports import AuditLogger
from app.shared_kernel.domain.exceptions import EntityNotFoundError, ValidationError

__all__ = [
    "ACTION_GRANT",
    "ACTION_REVOKE",
    "LAST_SUPERUSER",
    "SEED_PERMISSIONS_MISSING",
    "SuperuserInstaller",
    "SuperuserResult",
]

SEED_PERMISSIONS_MISSING: Final = "seed_permissions_missing"
LAST_SUPERUSER: Final = "last_superuser"
ACTION_GRANT: Final = "iam.superuser.grant"
ACTION_REVOKE: Final = "iam.superuser.revoke"

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SuperuserResult:
    """Outcome of one installer call."""

    telegram_user_id: int
    user_id: UUID
    changed: bool
    created: bool = False
    cache_invalidated: bool = True


def _state(user: User) -> dict[str, Any]:
    return {
        "telegram_user_id": user.telegram_user_id,
        "telegram_user_name": user.telegram_user_name,
        "is_superuser": user.is_superuser,
        "is_active": user.is_active,
        "is_deleted": user.is_deleted,
    }


class SuperuserInstaller:
    """Grant/revoke the superuser flag in its own transaction (commits the session)."""

    def __init__(
        self,
        session: AsyncSession,
        permissions: PermissionService,
        *,
        audit: AuditLogger | None = None,
        repository: IamAdminRepository | None = None,
    ) -> None:
        self._session = session
        self._permissions = permissions
        self._audit_logger: AuditLogger = audit or SqlAuditLogger()
        self._repo = repository or IamAdminRepository(session)

    async def grant(self, telegram_user_id: int, username: str | None = None) -> SuperuserResult:
        """Create, restore + activate, or flag an existing user as an active superuser.

        Requires the seeded permission catalogue (``seed_permissions_missing`` otherwise).
        Idempotent: the requested state already holds -> no write, no audit.
        """
        validate_telegram_user_id(telegram_user_id)
        name = normalize_username(username)
        async with self._transaction():
            await self._ensure_seed()
            user, before, created = await self._apply_grant(telegram_user_id, name)
            changed = created or before != _state(user)
            if changed:
                await self._session.flush()
                await self._audit(ACTION_GRANT, user, before, created=created)
        return await self._result(user, changed=changed, created=created)

    async def revoke(self, telegram_user_id: int, *, force: bool = False) -> SuperuserResult:
        """Clear the flag; the last active superuser is kept unless ``force``."""
        validate_telegram_user_id(telegram_user_id)
        async with self._transaction():
            # Lock every active superuser first (stable order): concurrent revokes serialize
            # here instead of both seeing "another superuser is left".
            active = await self._lock_active_superusers()
            user = await self._repo.get_user_by_telegram_id(
                telegram_user_id, include_deleted=True, for_update=True
            )
            if user is None:
                raise EntityNotFoundError("User not found", code=USER_NOT_FOUND)
            changed = user.is_superuser
            if changed:
                if not force and active == [user.id]:
                    raise ValidationError(
                        "Refusing to revoke the last active superuser (use --force)",
                        code=LAST_SUPERUSER,
                    )
                before = _state(user)
                user.is_superuser = False
                await self._session.flush()
                await self._audit(ACTION_REVOKE, user, before, force=force)
        return await self._result(user, changed=changed)

    # --- internals --------------------------------------------------------------------

    async def _apply_grant(
        self, telegram_user_id: int, name: str | None
    ) -> tuple[User, dict[str, Any] | None, bool]:
        user = await self._repo.get_user_by_telegram_id(
            telegram_user_id, include_deleted=True, for_update=True
        )
        if user is None:
            user = User(
                telegram_user_id=telegram_user_id,
                telegram_user_name=name,
                is_superuser=True,
                is_active=True,
                is_deleted=False,
            )
            self._session.add(user)
            await self._session.flush()
            return user, None, True
        before = _state(user)
        if user.is_deleted:
            user.is_deleted = False
        if not user.is_active:
            user.is_active = True
        if not user.is_superuser:
            user.is_superuser = True
        if name is not None and user.telegram_user_name != name:
            user.telegram_user_name = name
        return user, before, False

    async def _ensure_seed(self) -> None:
        stmt = select(Permission.slug).where(Permission.slug.in_(SEED_PERMISSION_SLUGS))
        found = set((await self._session.scalars(stmt)).all())
        missing = SEED_PERMISSION_SLUGS - found
        if missing:
            raise EntityNotFoundError(
                f"{len(missing)} seeded permissions are missing: apply migrations (make migrate)",
                code=SEED_PERMISSIONS_MISSING,
            )

    async def _lock_active_superusers(self) -> list[UUID]:
        stmt = (
            select(User.id)
            .where(
                User.is_superuser.is_(True),
                User.is_active.is_(True),
                User.is_deleted.is_(False),
            )
            .order_by(User.id)
            .with_for_update()
        )
        return list((await self._session.scalars(stmt)).all())

    async def _audit(
        self,
        action: str,
        user: User,
        before: Mapping[str, Any] | None,
        **flags: bool,
    ) -> None:
        await self._audit_logger.record(
            self._session,
            AuditEvent(
                actor_type="system",
                action=action,
                entity_type="user",
                entity_id=user.id,
                payload={"before": before, "after": _state(user), **flags},
                result="success",
            ),
        )

    async def _result(self, user: User, *, changed: bool, created: bool = False) -> SuperuserResult:
        ok = True
        if changed:
            ok = await self._invalidate(user.telegram_user_id)
        return SuperuserResult(
            telegram_user_id=user.telegram_user_id,
            user_id=user.id,
            changed=changed,
            created=created,
            cache_invalidated=ok,
        )

    async def _invalidate(self, telegram_user_id: int) -> bool:
        """AFTER commit only; Redis problems never undo the stored change."""
        try:
            ok = (await self._permissions.invalidate(telegram_user_id)).ok
        except Exception as error:  # noqa: BLE001 - cache is best effort after commit
            _log.warning(
                "superuser_cache_invalidate_error",
                telegram_user_id=telegram_user_id,
                error_type=type(error).__name__,
            )
            ok = False
        if not ok:
            iam_cache_invalidation_failures_total.inc(scope="user")
        return ok

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        for name in ("new", "dirty", "deleted"):
            if getattr(self._session, name, None):
                raise ValidationError(
                    "Session holds foreign pending changes", code=SESSION_NOT_CLEAN
                )
        try:
            yield
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise map_integrity_error(error) from error
        except BaseException:
            await self._session.rollback()
            raise
