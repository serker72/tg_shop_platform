"""Proxy pool use cases (decisions.md "Пул прокси", docs/ops/bot-runtime.md).

Transaction discipline is the one of the IAM admin services: every writing method commits
the passed session itself (it must hold no pending changes - ``session_not_clean``), any
error rolls back, rows are locked with ``FOR UPDATE`` + ``populate_existing``, audit is
written in the same transaction, constraint errors become domain errors.

**Lock order: proxy row, then bot row** (``assign_proxy``, ``register_strike``, ``ban``),
so parallel assignment and quarantine/ban never deadlock. The number of bots of a proxy is
never stored: ``COUNT(bots WHERE proxy_id AND NOT is_deleted)`` is recounted under the
``FOR UPDATE`` lock of the proxy row, so parallel assignments never exceed
``PROXY_MAX_BOTS_PER_IP``.
"""

from __future__ import annotations

import enum
import hashlib
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol
from uuid import UUID

import structlog
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import ProxySettings
from app.core.proxy_url import mask_proxy_url, validate_proxy_url
from app.modules.bot_manager.metrics import LabeledCounter, proxy_failure_verdicts_total
from app.modules.bot_manager.models import (
    PROXY_STATUS_ACTIVE,
    PROXY_STATUS_BANNED,
    PROXY_STATUS_QUARANTINE,
    Proxy,
)
from app.modules.bot_manager.proxy_health import ProxyHealthTracker
from app.modules.bot_manager.proxy_repository import ProxyRepository
from app.modules.bot_manager.repository import BotRepository
from app.modules.iam.admin_service import CONFLICT, SESSION_NOT_CLEAN, map_integrity_error
from app.modules.iam.permissions import PROXIES_MANAGE
from app.modules.logging.audit import SqlAuditLogger
from app.modules.logging.events import AuditEvent
from app.modules.logging.ports import AuditLogger
from app.shared_kernel.domain.exceptions import (
    DomainError,
    EntityAlreadyExistsError,
    EntityNotFoundError,
    ValidationError,
)

__all__ = [
    "BOT_NOT_FOUND",
    "INVALID_PROXY_URL",
    "NO_PROXY_AVAILABLE",
    "PROXY_EXISTS",
    "PROXY_NOT_FOUND",
    "FailureVerdict",
    "NoProxyAvailableError",
    "PermissionChecker",
    "ProxyPoolService",
    "StrikeResult",
    "VerdictKind",
    "proxy_url_hash",
]

BOT_NOT_FOUND: Final = "bot_not_found"
INVALID_PROXY_URL: Final = "invalid_proxy_url"
NO_PROXY_AVAILABLE: Final = "no_proxy_available"
PROXY_EXISTS: Final = "proxy_exists"
PROXY_NOT_FOUND: Final = "proxy_not_found"
_LAST_ERROR_MAX: Final = 500
#: Re-reads of ``bots.proxy_id`` when it changes concurrently during ``assign_proxy``.
_ASSIGN_ATTEMPTS: Final = 3

VerdictKind = Literal["strike", "outage", "single_proxy", "deduplicated", "error"]
_log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def proxy_url_hash(url: str) -> str:
    """sha256 of the normalized URL (``proxies.url_hash``)."""
    return hashlib.sha256(validate_proxy_url(url).encode()).hexdigest()


class NoProxyAvailableError(DomainError):
    """No ACTIVE proxy has a free slot."""

    def __init__(self, message: str = "No proxy available", *, code: str | None = None) -> None:
        super().__init__(message, code=code or NO_PROXY_AVAILABLE)


class PermissionChecker(Protocol):
    async def require(
        self, telegram_user_id: int, slug: str, tenant_id: UUID | None = None
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class StrikeResult:
    """``applied=False`` - deduplicated (one strike per window) or the proxy is not ACTIVE."""

    applied: bool
    quarantined: bool = False
    released_bot_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class FailureVerdict:
    """Decision of :meth:`ProxyPoolService.report_failure`; ``released_bot_ids`` - bots
    unbound by a quarantine (to restart / reassign, task 11)."""

    verdict: VerdictKind
    released_bot_ids: tuple[UUID, ...] = ()


class _Bind(enum.Enum):
    BOUND = "bound"
    UNAVAILABLE = "unavailable"
    CHANGED = "changed"


class _Abort(Exception):  # noqa: N818 - internal control-flow signal
    def __init__(self, outcome: _Bind) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


class ProxyPoolService:
    """Assignment, strikes, quarantine and manual management of the proxy pool.

    Bound to one session; not for concurrent use.
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: ProxySettings,
        tracker: ProxyHealthTracker,
        *,
        permissions: PermissionChecker | None = None,
        audit: AuditLogger | None = None,
        clock: Callable[[], datetime] = _utcnow,
        verdicts: LabeledCounter = proxy_failure_verdicts_total,
    ) -> None:
        self._session = session
        self._settings = settings
        self._tracker = tracker
        self._permissions = permissions
        self._audit_logger: AuditLogger = audit or SqlAuditLogger()
        self._clock = clock
        self._verdicts = verdicts
        self._repo = ProxyRepository(session)
        self._bots = BotRepository(session)

    # --- plumbing ---------------------------------------------------------------------
    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        session = self._session
        if session.new or session.dirty or session.deleted:
            raise DomainError("Session has pending changes", code=SESSION_NOT_CLEAN)
        try:
            yield
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            if constraint == "uq_proxies_url_hash":
                raise EntityAlreadyExistsError("Proxy already exists", code=PROXY_EXISTS) from None
            raise map_integrity_error(error) from None
        except BaseException:
            await session.rollback()
            raise

    async def _require(self, actor: int) -> None:
        if self._permissions is None:
            raise RuntimeError("ProxyPoolService needs a PermissionChecker for manual actions")
        await self._permissions.require(actor, PROXIES_MANAGE)

    async def _audit(
        self, action: str, proxy: Proxy, payload: Mapping[str, Any], *, actor: int | None = None
    ) -> None:
        await self._audit_logger.record(
            self._session,
            AuditEvent(
                actor_type="system" if actor is None else "user",
                actor_id=actor,
                action=action,
                entity_type="proxy",
                entity_id=proxy.id,
                payload=dict(payload),
            ),
        )

    async def _locked_proxy(self, proxy_id: UUID) -> Proxy:
        proxy = await self._repo.get(proxy_id, for_update=True)
        if proxy is None:
            raise EntityNotFoundError("Proxy not found", code=PROXY_NOT_FOUND)
        return proxy

    # --- assignment -------------------------------------------------------------------
    async def get_available_proxy(self) -> Proxy | None:
        """Best ACTIVE proxy with a free slot (strikes, load, id); read only, no commit."""
        return await self._repo.pick_available(self._settings.proxy_max_bots_per_ip)

    async def _try_bind(
        self, bot_id: UUID, proxy_id: UUID, *, expected: UUID | None, keep: bool
    ) -> _Bind:
        """In a savepoint: lock the proxy, recount, lock the bot, re-check its proxy and
        bind. A rejected proxy releases its lock (savepoint rollback)."""
        limit = self._settings.proxy_max_bots_per_ip
        try:
            async with self._session.begin_nested():
                proxy = await self._repo.get(proxy_id, for_update=True)
                if proxy is None or proxy.status != PROXY_STATUS_ACTIVE:
                    raise _Abort(_Bind.UNAVAILABLE)
                count = await self._repo.bot_count(proxy_id)
                # ``keep``: the bot is already counted - stay while not overfull.
                if count > limit or (not keep and count >= limit):
                    raise _Abort(_Bind.UNAVAILABLE)
                bot = await self._bots.get(bot_id, for_update=True)
                if bot is None:
                    raise EntityNotFoundError("Bot not found", code=BOT_NOT_FOUND)
                if bot.proxy_id != expected:
                    raise _Abort(_Bind.CHANGED)
                if not keep:
                    bot.proxy_id = proxy_id
                    await self._session.flush()
        except _Abort as abort:
            return abort.outcome
        return _Bind.BOUND

    async def assign_proxy(self, bot_id: UUID) -> UUID:
        """Bind the bot to a proxy (sticky) and commit; :class:`NoProxyAvailableError`."""
        limit = self._settings.proxy_max_bots_per_ip
        async with self._transaction():
            for _ in range(_ASSIGN_ATTEMPTS):
                row = await self._repo.bot_proxy_id(bot_id)
                if row is None:
                    raise EntityNotFoundError("Bot not found", code=BOT_NOT_FOUND)
                (current,) = row
                if current is not None:
                    outcome = await self._try_bind(bot_id, current, expected=current, keep=True)
                    if outcome is _Bind.BOUND:
                        return current
                    if outcome is _Bind.CHANGED:
                        continue
                changed = False
                for candidate in await self._repo.available_ids(limit, exclude=current):
                    outcome = await self._try_bind(bot_id, candidate, expected=current, keep=False)
                    if outcome is _Bind.BOUND:
                        _log.info("proxy_assigned", bot_id=str(bot_id), proxy_id=str(candidate))
                        return candidate
                    if outcome is _Bind.CHANGED:
                        changed = True
                        break
                if not changed:
                    raise NoProxyAvailableError
            raise DomainError("Bot proxy changed concurrently", code=CONFLICT)
        raise AssertionError("unreachable")  # pragma: no cover

    async def release_proxy(self, bot_id: UUID) -> UUID | None:
        """``bots.proxy_id = NULL``; returns the previous proxy id."""
        async with self._transaction():
            bot = await self._bots.get(bot_id, for_update=True)
            if bot is None:
                raise EntityNotFoundError("Bot not found", code=BOT_NOT_FOUND)
            previous = bot.proxy_id
            if previous is not None:
                bot.proxy_id = None
                await self._session.flush()
        return previous

    # --- failures ---------------------------------------------------------------------
    async def report_failure(
        self, proxy_id: UUID, exc: BaseException | None = None
    ) -> FailureVerdict:
        """Strike only when the other ACTIVE proxies work (pool correlation).

        Fail-closed: a Redis or database error (deadlock included) gives ``error`` - no
        strike, logged and counted, never raised to the poller.
        """
        reason = type(exc).__name__ if exc is not None else "failure"
        try:
            verdict = await self._decide(proxy_id, reason)
        except (RedisError, OSError, SQLAlchemyError) as error:
            _log.warning(
                "proxy_failure_verdict_error", proxy_id=str(proxy_id), error=type(error).__name__
            )
            verdict = FailureVerdict("error")
        self._verdicts.inc(verdict.verdict)
        return verdict

    async def _decide(self, proxy_id: UUID, reason: str) -> FailureVerdict:
        async with self._transaction():
            active = await self._repo.active_ids()
        if proxy_id not in active:
            return FailureVerdict("deduplicated")  # already quarantined / banned
        if len(active) == 1:
            _log.warning("proxy_single_failure", proxy_id=str(proxy_id), reason=reason)
            return FailureVerdict("single_proxy")
        others = [item for item in active if item != proxy_id]
        health = await self._tracker.pool_health(others)
        if health.ok == 0 or health.failed / health.total >= self._settings.proxy_outage_ratio:
            await self._tracker.set_outage()
            _log.warning(
                "telegram_outage_suspected",
                proxy_id=str(proxy_id),
                ok_others=health.ok,
                failed_others=health.failed,
                others=health.total,
            )
            return FailureVerdict("outage")
        strike = await self.register_strike(proxy_id, reason, allow_fallback=False)
        if not strike.applied:
            return FailureVerdict("deduplicated")
        return FailureVerdict("strike", strike.released_bot_ids)

    async def register_strike(
        self, proxy_id: UUID, reason: str, *, allow_fallback: bool = True
    ) -> StrikeResult:
        """At most one strike per window (Redis ``SET NX EX``); ``>=
        PROXY_STRIKES_TO_QUARANTINE`` -> quarantine + all bots unbound.

        Redis unavailable: ``allow_fallback=True`` (direct call) dedups by ``last_strike_at``
        in PostgreSQL, ``False`` (from :meth:`report_failure`) re-raises. A failed
        transaction frees the taken dedup key (best effort).
        """
        try:
            first: bool | None = await self._tracker.try_acquire_strike(proxy_id)
        except (RedisError, OSError) as error:
            if not allow_fallback:
                raise
            _log.warning("proxy_strike_dedup_fallback", error=type(error).__name__)
            first = None
        if first is False:
            return StrikeResult(applied=False)
        try:
            return await self._apply_strike(proxy_id, reason, pg_dedup=first is None)
        except BaseException:
            if first:
                await self._tracker.release_strike(proxy_id)
            raise

    async def _apply_strike(self, proxy_id: UUID, reason: str, *, pg_dedup: bool) -> StrikeResult:
        window = timedelta(seconds=self._settings.proxy_strike_window_seconds)
        async with self._transaction():
            proxy = await self._repo.get(proxy_id, for_update=True)
            if proxy is None or proxy.status != PROXY_STATUS_ACTIVE:
                return StrikeResult(applied=False)
            now = self._clock()
            if pg_dedup and proxy.last_strike_at is not None:
                if now - proxy.last_strike_at < window:
                    return StrikeResult(applied=False)
            proxy.strike_count += 1
            proxy.last_strike_at = now
            proxy.last_error = reason[:_LAST_ERROR_MAX]
            released: tuple[UUID, ...] = ()
            quarantined = proxy.strike_count >= self._settings.proxy_strikes_to_quarantine
            if quarantined:
                proxy.status = PROXY_STATUS_QUARANTINE
                proxy.last_ban_at = now
                released = await self._repo.release_bots(proxy_id)
            await self._session.flush()
            await self._audit(
                "proxy.quarantine" if quarantined else "proxy.strike",
                proxy,
                {
                    "reason": proxy.last_error,
                    "strike_count": proxy.strike_count,
                    "released_bots": [str(item) for item in released],
                },
            )
        _log.warning(
            "proxy_strike",
            proxy_id=str(proxy_id),
            strike_count=proxy.strike_count,
            quarantined=quarantined,
            released_bots=len(released),
        )
        return StrikeResult(applied=True, quarantined=quarantined, released_bot_ids=released)

    async def rehabilitate(self, now: datetime | None = None) -> int:
        """Quarantine older than ``PROXY_QUARANTINE_DAYS`` -> active; idempotent."""
        moment = now or self._clock()
        cutoff = moment - timedelta(days=self._settings.proxy_quarantine_days)
        async with self._transaction():
            count = await self._repo.rehabilitate(cutoff)
        if count:
            _log.info("proxies_rehabilitated", count=count)
        return count

    # --- manual management (P5) -------------------------------------------------------
    async def add(self, url: str, label: str | None, actor: int) -> UUID:
        """Add a proxy; the URL is normalized (scheme/host lower-cased) before hashing."""
        await self._require(actor)
        try:
            value = validate_proxy_url(url)
        except ValueError as error:
            raise ValidationError(str(error), code=INVALID_PROXY_URL) from None
        digest = proxy_url_hash(value)
        clean_label = label.strip() or None if label else None
        async with self._transaction():
            if await self._repo.by_url_hash(digest) is not None:
                raise EntityAlreadyExistsError("Proxy already exists", code=PROXY_EXISTS)
            proxy = await self._repo.add(Proxy(url=value, url_hash=digest, label=clean_label))
            await self._audit(
                "proxy.add",
                proxy,
                {"proxy": mask_proxy_url(value), "label": clean_label},
                actor=actor,
            )
        return proxy.id

    async def ban(self, proxy_id: UUID, actor: int) -> tuple[UUID, ...]:
        """Manual ban; unbinds the bots and returns their ids."""
        await self._require(actor)
        async with self._transaction():
            proxy = await self._locked_proxy(proxy_id)
            if proxy.status == PROXY_STATUS_BANNED:
                return ()
            before = proxy.status
            proxy.status = PROXY_STATUS_BANNED
            proxy.last_ban_at = self._clock()
            released = await self._repo.release_bots(proxy_id)
            await self._session.flush()
            await self._audit(
                "proxy.ban",
                proxy,
                {"before": before, "released_bots": [str(item) for item in released]},
                actor=actor,
            )
        return released

    async def activate(self, proxy_id: UUID, actor: int) -> None:
        """Manual return to ACTIVE; resets ``strike_count``."""
        await self._require(actor)
        async with self._transaction():
            proxy = await self._locked_proxy(proxy_id)
            if proxy.status == PROXY_STATUS_ACTIVE and proxy.strike_count == 0:
                return
            before = {"status": proxy.status, "strike_count": proxy.strike_count}
            proxy.status = PROXY_STATUS_ACTIVE
            proxy.strike_count = 0
            await self._session.flush()
            await self._audit("proxy.activate", proxy, {"before": before}, actor=actor)
