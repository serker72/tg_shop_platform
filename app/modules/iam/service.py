"""Permission checks: ``PermissionService.has_permission`` with Redis cache-aside.

Flow for one request: snapshot memoised per user in the service instance (REQUEST scope),
so one ``MGET``/``EVAL`` pair or one PostgreSQL load serves every check of the request.
Miss -> load from PostgreSQL -> cache for
``min(iam_cache_ttl_seconds, seconds until the earliest override expiry)`` (unknown users -
:data:`NEGATIVE_CACHE_TTL_SECONDS`) -> decide. Redis down -> PostgreSQL on every request.

Nothing is cached when the caller's session holds pending changes (risk RA-1): the loaded
snapshot could mix flushed and uncommitted state, and a concurrent reader would then see it.

Invalidation (after ``session.commit()`` only - otherwise a concurrent reader caches the old
state again) returns :class:`InvalidationResult`; ``ok=False`` means Redis did not confirm
it and the write service must retry (or rely on its outbox):

* :meth:`PermissionService.invalidate` (one user): a user was created, a role was
  granted/revoked, an override changed, ``tenant_moderators`` changed, the user was
  deactivated/deleted/restored, the superuser flag changed;
* :meth:`PermissionService.invalidate_all`: permissions of a role changed, role
  deactivated/deleted, permission deactivated/deleted, shop deleted/restored.

Known limitation: there is no single-flight warm-up. Several concurrent requests for the
same user with a cold cache all read PostgreSQL and all try the guarded write; the write is
harmless (generations check it) but the load is duplicated. Acceptable for the expected
staff-facing traffic; a per-key lock could be added later.

The snapshot only says *which* shops the user moderates: an action on a shop must load the
shop first (existence, ``is_deleted``, settings) - ``has_permission`` never does.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam.cache import InvalidationResult, PermissionCache
from app.modules.iam.effective_permissions import EffectivePermissionsReport
from app.modules.iam.models import PERMISSION_SLUG_PATTERN
from app.modules.iam.repository import PermissionQueryRepository
from app.modules.iam.snapshot import Decision, PermissionSnapshot
from app.modules.system.cache import SystemSettingsCache
from app.modules.system.models import DEFAULT_IAM_CACHE_TTL_SECONDS
from app.modules.system.repository import SystemSettingsRepository
from app.shared_kernel.domain.exceptions import (
    EntityNotFoundError,
    PermissionDeniedError,
    ValidationError,
)

__all__ = [
    "DEFAULT_GENERATIONS",
    "INVALID_PERMISSION_SLUG",
    "NEGATIVE_CACHE_TTL_SECONDS",
    "PERMISSION_DENIED",
    "PermissionService",
    "InvalidationResult",
]

#: TTL of a cached "unknown user" snapshot (decision 3A).
NEGATIVE_CACHE_TTL_SECONDS = 60
#: ``DomainError.code`` of :meth:`PermissionService.require`.
PERMISSION_DENIED = "permission_denied"
#: ``DomainError.code`` of a malformed permission slug.
INVALID_PERMISSION_SLUG = "invalid_permission_slug"
#: Generations assumed when Redis never answered (defaults of untouched keys).
DEFAULT_GENERATIONS: tuple[int, int] = (0, 0)

_SLUG_RE = re.compile(PERMISSION_SLUG_PATTERN)
_log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PermissionService:
    """Request-scoped permission checker working on the caller's session (read only)."""

    def __init__(
        self,
        session: AsyncSession,
        cache: PermissionCache,
        *,
        queries: PermissionQueryRepository | None = None,
        system_settings: SystemSettingsRepository | None = None,
        settings_cache: SystemSettingsCache | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        """``settings_cache`` (DI wiring) supplies ``iam_cache_ttl_seconds`` from the Redis
        settings snapshot; without it the TTL is read from ``system_settings`` in the request
        session (CLI / tests building the service by hand)."""
        self._session = session
        self._cache = cache
        self._queries = queries or PermissionQueryRepository(session)
        self._system_settings = system_settings or SystemSettingsRepository(session)
        self._settings_cache = settings_cache
        self._clock = clock
        self._snapshots: dict[int, PermissionSnapshot] = {}

    async def get_snapshot(self, telegram_user_id: int) -> PermissionSnapshot:
        """Snapshot for this request: cache -> PostgreSQL; memoised per user on success."""
        memoised = self._snapshots.get(telegram_user_id)
        if memoised is not None:
            return memoised
        cached = await self._cache.get_with_generations(telegram_user_id)
        if cached is not None:
            self._snapshots[telegram_user_id] = cached[0]
            return cached[0]
        generations = await self._cache.read_generations(telegram_user_id)
        cache_writable = generations is not None
        if cache_writable and self._session_dirty():
            # Pending (uncommitted) changes: do not publish a possibly mixed state.
            _log.debug("permission_cache_skip_dirty_session", telegram_user_id=telegram_user_id)
            cache_writable = False
        now = self._clock()
        snapshot = await self._queries.load_snapshot(telegram_user_id, now=now)
        self._snapshots[telegram_user_id] = snapshot
        if cache_writable:
            ttl = await self._ttl_for(snapshot, now)
            await self._cache.set(
                snapshot,
                telegram_user_id,
                ttl,
                generations=generations or DEFAULT_GENERATIONS,
            )
        return snapshot

    async def check(
        self, telegram_user_id: int, slug: str, tenant_id: UUID | None = None
    ) -> Decision:
        """Full decision (allowed + source + source roles)."""
        self._validate_slug(slug)
        snapshot = await self.get_snapshot(telegram_user_id)
        return snapshot.decide(slug, tenant_id)

    async def has_permission(
        self, telegram_user_id: int, slug: str, tenant_id: UUID | None = None
    ) -> bool:
        """``True`` if the user holds ``slug`` (inside shop ``tenant_id`` when given)."""
        return (await self.check(telegram_user_id, slug, tenant_id)).allowed

    async def require(
        self, telegram_user_id: int, slug: str, tenant_id: UUID | None = None
    ) -> Decision:
        """Return the decision or raise :class:`PermissionDeniedError` (``permission_denied``).

        The message names only the requested permission; the reason is logged.
        """
        decision = await self.check(telegram_user_id, slug, tenant_id)
        if not decision.allowed:
            _log.info(
                "permission_denied",
                telegram_user_id=telegram_user_id,
                permission=slug,
                tenant_id=None if tenant_id is None else str(tenant_id),
                source=decision.source.value,
            )
            raise PermissionDeniedError(f"Permission denied: {slug}", code=PERMISSION_DENIED)
        return decision

    async def effective_permissions(self, telegram_user_id: int) -> EffectivePermissionsReport:
        """Why the user holds each permission: roles, overrides, expired overrides.

        Always reads PostgreSQL (decision 2-a), never the cache: an administrative report
        must show the committed state. Every ``allowed`` value is computed by
        :meth:`PermissionSnapshot.decide` - the same rule :meth:`check` uses - so the report
        and a real permission check cannot disagree.
        """
        return await self._queries.load_effective_permissions(telegram_user_id, now=self._clock())

    async def invalidate(self, telegram_user_id: int) -> InvalidationResult:
        """Drop the user's snapshot. Call AFTER ``session.commit()``.

        ``ok=False`` -> the change is not visible to other processes consistently: retry
        (or let the outbox retry). Logged as ``error``.
        """
        self._snapshots.pop(telegram_user_id, None)
        result = await self._cache.invalidate(telegram_user_id)
        if not result.ok:
            _log.error("permission_cache_invalidate_unconfirmed", telegram_user_id=telegram_user_id)
        return result

    async def invalidate_all(self) -> InvalidationResult:
        """Drop every snapshot. Call AFTER ``session.commit()``; ``ok=False`` -> retry."""
        self._snapshots.clear()
        result = await self._cache.invalidate_all()
        if not result.ok:
            _log.error("permission_cache_invalidate_all_unconfirmed")
        return result

    # --- internals --------------------------------------------------------------------

    def _session_dirty(self) -> bool:
        """``True`` if the session holds pending changes - or cannot be inspected (fail-closed).

        ``None`` instead of an ``IdentitySet`` means the session object is not a real
        ``AsyncSession`` (broken wiring); caching then must not happen.
        """
        for name in ("new", "dirty", "deleted"):
            pending = getattr(self._session, name, None)
            if pending is None or pending:
                return True
        return False

    @staticmethod
    def _validate_slug(slug: str) -> None:
        # Format only: the catalogue grows with migrations, unknown slugs are just denied.
        if not _SLUG_RE.fullmatch(slug):
            raise ValidationError(
                f"Malformed permission slug: {slug!r}", code=INVALID_PERMISSION_SLUG
            )

    async def _ttl_for(self, snapshot: PermissionSnapshot, now: datetime) -> int:
        if not snapshot.exists:
            return NEGATIVE_CACHE_TTL_SECONDS
        if self._settings_cache is not None:
            # Never raises: Redis -> PostgreSQL -> last known -> default.
            ttl = (await self._settings_cache.get()).iam_cache_ttl_seconds
        else:
            try:
                ttl = (await self._system_settings.get()).iam_cache_ttl_seconds
            except (EntityNotFoundError, SQLAlchemyError):
                _log.warning("iam_cache_ttl_fallback", ttl=DEFAULT_IAM_CACHE_TTL_SECONDS)
                ttl = DEFAULT_IAM_CACHE_TTL_SECONDS
        if snapshot.next_expiry is not None:
            ttl = min(ttl, math.ceil((snapshot.next_expiry - now).total_seconds()))
        return max(1, ttl)
