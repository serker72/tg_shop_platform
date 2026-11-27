"""Translate persisted desired state into locally leased polling runners."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.modules.bot_manager.lease import BotLease, worker_identity
from app.modules.bot_manager.models import (
    BOT_STATUS_ERROR,
    BOT_STATUS_INVALID_TOKEN,
    BOT_STATUS_RUNNING,
    BOT_STATUS_STOPPED,
    PROXY_STATUS_ACTIVE,
    Bot,
    Proxy,
)
from app.modules.bot_manager.proxy_health import ProxyHealthTracker
from app.modules.bot_manager.proxy_service import NoProxyAvailableError, ProxyPoolService
from app.modules.bot_manager.repository import BotRepository
from app.modules.bot_manager.runtime import FinalStatusWriteError
from app.modules.iam.models import Tenant
from app.shared_kernel.domain.exceptions import EntityNotFoundError
from app.shared_kernel.infrastructure.generational_cache import REDIS_ERRORS

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from aiogram import Dispatcher
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.config import Settings
    from app.modules.bot_manager.commands import BotCommand
    from app.modules.bot_manager.runtime import BotRunner, BotSupervisor

_log = structlog.get_logger(__name__)
# Lease loss is handled separately from database reconciliation so a blocked
# transaction cannot keep an expired runner polling.
_RETRYABLE = (*REDIS_ERRORS, SQLAlchemyError, TimeoutError, FinalStatusWriteError)


class WorkerUnavailableError(RuntimeError):
    """The worker did not converge now; retry or let reconciliation repair it."""


class _LeaseLost(Exception):
    pass


class _Ineligible(Exception):
    pass


@dataclass(slots=True)
class _Slot:
    lease: BotLease
    deadline: float
    runner: BotRunner | None = None
    completion: asyncio.Future[None] | None = None
    token_hash: str | None = None
    restart_revision: int | None = None
    proxy_id: UUID | None = None
    lost: bool = False


class BotWorker:
    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
        supervisor: BotSupervisor,
        dispatcher: Dispatcher,
        *,
        owner: str | None = None,
        on_no_proxy: Callable[[], None] | None = None,
    ) -> None:
        self.owner = owner or worker_identity()
        self._redis, self._sessions = redis, session_factory
        self._supervisor, self._dispatcher = supervisor, dispatcher
        self._ttl = settings.bot_worker.lease_ttl_seconds
        self._interval = settings.bot_worker.reconcile_interval_seconds
        self._proxy = settings.proxy
        self._health = ProxyHealthTracker(
            redis, window_seconds=self._proxy.proxy_strike_window_seconds
        )
        self._slots: dict[UUID, _Slot] = {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._closing = False
        self._on_no_proxy = on_no_proxy

    async def start(self, command: BotCommand) -> None:
        await self._dispatch(command)

    async def stop(self, command: BotCommand) -> None:
        await self._dispatch(command)

    async def restart(self, command: BotCommand) -> None:
        await self._dispatch(command)

    async def _dispatch(self, command: BotCommand) -> None:
        try:
            async with self._lock:
                if self._closing:
                    raise WorkerUnavailableError("worker shutting down")
                await self._sync(command.bot_id, command=command)
        except _RETRYABLE as exc:
            raise WorkerUnavailableError(type(exc).__name__) from None

    async def reconcile(self, *, strict: bool = False) -> None:
        """Run one pass. Commands and runner transitions are serialized locally."""
        async with self._lock:
            if self._closing:
                return
            async with self._sessions() as session:
                ids = [bot.id for bot in await BotRepository(session).list_runnable()]
                repairs = list(
                    await session.scalars(
                        select(Bot.id)
                        .join(Tenant, Tenant.id == Bot.tenant_id)
                        .where(
                            Bot.status.not_in((BOT_STATUS_STOPPED, BOT_STATUS_INVALID_TOKEN)),
                            or_(
                                Bot.is_deleted, ~Bot.is_active, Tenant.is_deleted, ~Tenant.is_active
                            ),
                        )
                    )
                )
            for bot_id in dict.fromkeys((*self._slots, *ids)):
                if self._closing:
                    break
                try:
                    await self._sync(bot_id)
                except _RETRYABLE as exc:
                    if strict:
                        raise
                    _log.warning(
                        "bot_reconcile_failed", bot_id=str(bot_id), error=type(exc).__name__
                    )
            for bot_id in repairs:
                if self._closing:
                    break
                if bot_id not in self._slots:
                    await self._repair_ineligible(bot_id)

    async def reconcile_loop(self) -> None:
        while not self._closing:
            try:
                async with asyncio.timeout(self._interval):
                    await self._wake.wait()
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.reconcile()
            except _RETRYABLE as exc:
                _log.warning("bot_reconcile_scan_failed", error=type(exc).__name__)

    async def renew_loop(self) -> None:
        while True:
            # Each RPC has its own deadline; renewal never acquires the lifecycle lock.
            await asyncio.sleep(self._ttl / 4)
            await asyncio.gather(*(self._renew(slot) for slot in tuple(self._slots.values())))

    def _lose(self, slot: _Slot) -> None:
        if not slot.lost:
            slot.lost = True
            if slot.runner is not None:
                slot.runner.request_stop()
            self._wake.set()

    async def _renew(self, slot: _Slot) -> None:
        if slot.lost:
            return
        now = asyncio.get_running_loop().time()
        try:
            async with asyncio.timeout_at(min(slot.deadline, now + self._ttl / 4)):
                kept = now < slot.deadline and await slot.lease.renew()
        except (*REDIS_ERRORS, TimeoutError):
            kept = False
        if kept and not slot.lost and asyncio.get_running_loop().time() < slot.deadline:
            slot.deadline = now + self._ttl
        else:
            self._lose(slot)

    async def _require_lease(self, slot: _Slot) -> None:
        now = asyncio.get_running_loop().time()
        try:
            async with asyncio.timeout_at(min(slot.deadline, now + self._ttl / 4)):
                owned = not slot.lost and now < slot.deadline and await slot.lease.is_owner()
        except (*REDIS_ERRORS, TimeoutError):
            owned = False
        if not owned or slot.lost or asyncio.get_running_loop().time() >= slot.deadline:
            self._lose(slot)
            raise _LeaseLost

    @staticmethod
    def _reason(bot: Bot, tenant: Tenant | None) -> str | None:
        if tenant is None or tenant.is_deleted:
            return "tenant_deleted"
        if not tenant.is_active:
            return "tenant_inactive"
        if bot.is_deleted or not bot.is_active:
            return "bot_inactive"
        return None

    async def _read(self, bot_id: UUID) -> tuple[Bot, str | None, Proxy | None] | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(Bot, Tenant, Proxy)
                    .join(Tenant, Tenant.id == Bot.tenant_id)
                    .outerjoin(Proxy, Proxy.id == Bot.proxy_id)
                    .where(Bot.id == bot_id)
                )
            ).one_or_none()
            if row is None:
                return None
            bot, tenant, proxy = row
            return bot, self._reason(bot, tenant), proxy if self._proxy.tenants_use_proxy else None

    async def _sync(self, bot_id: UUID, *, command: BotCommand | None = None) -> None:
        slot = self._slots.get(bot_id)
        if slot is None and command is not None and command.kind != "START_BOT":
            return
        state = await self._read(bot_id)
        bot, reason, proxy = state if state is not None else (None, None, None)
        runnable = bot is not None and reason is None and bot.status != BOT_STATUS_INVALID_TOKEN
        if command is not None and command.kind == "STOP_BOT" and runnable:
            # A newer START or tenant restore superseded this STOP.
            return
        if slot is not None:
            divergent = bot is not None and (
                slot.restart_revision != bot.restart_revision
                or slot.token_hash != bot.token_hash
                or (
                    self._proxy.tenants_use_proxy
                    and (
                        proxy is None
                        or proxy.id != slot.proxy_id
                        or proxy.status != PROXY_STATUS_ACTIVE
                    )
                )
            )
            if (
                slot.lost
                or not runnable
                or divergent
                or slot.completion is None
                or slot.completion.done()
            ):
                await self._retire(bot_id, slot)
                # Completion can have just committed invalid_token, or control
                # may have changed while we waited. _launch re-reads after acquire.
            else:
                if bot is not None and slot.runner is not None and slot.runner.polling_succeeded:
                    if bot.status != BOT_STATUS_RUNNING:
                        await self._write_status(slot, BOT_STATUS_RUNNING, None)
                return
        if (
            runnable
            and not self._closing
            and len(self._slots) < self._supervisor.max_bots
            and (command is None or command.kind != "STOP_BOT")
        ):
            await self._launch(bot_id)

    @staticmethod
    async def _lock_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant | None:
        return await session.get(Tenant, tenant_id, with_for_update=True, populate_existing=True)

    async def _repair_ineligible(self, bot_id: UUID) -> None:
        # This is desired-state repair, not a write made on behalf of a runner.
        async with self._sessions.begin() as session:
            tenant_id = await session.scalar(select(Bot.tenant_id).where(Bot.id == bot_id))
            if tenant_id is None:
                return
            tenant = await self._lock_tenant(session, tenant_id)
            repo = BotRepository(session)
            bot = await repo.get(bot_id, include_deleted=True, for_update=True)
            if bot is None or bot.tenant_id != tenant_id:
                return
            reason = self._reason(bot, tenant)
            if reason and bot.status not in (BOT_STATUS_STOPPED, BOT_STATUS_INVALID_TOKEN):
                await repo.set_status(bot, BOT_STATUS_STOPPED, error=reason)

    async def _launch(self, bot_id: UUID) -> None:
        lease = BotLease(self._redis, bot_id, self.owner, self._ttl)
        now = asyncio.get_running_loop().time()
        async with asyncio.timeout(self._ttl / 4):
            if not await lease.acquire():
                return
        slot = self._slots[bot_id] = _Slot(lease, now + self._ttl)
        try:
            state = await self._read(bot_id)
            if state is None or state[1] is not None or state[0].status == BOT_STATUS_INVALID_TOKEN:
                return
            if self._proxy.tenants_use_proxy:
                try:
                    async with self._sessions.begin() as session:
                        tenant_id = state[0].tenant_id
                        tenant = await self._lock_tenant(session, tenant_id)
                        if tenant is None or tenant.is_deleted or not tenant.is_active:
                            return

                        async def before_bind(locked_bot: Bot) -> None:
                            if (
                                locked_bot.tenant_id != tenant_id
                                or self._reason(locked_bot, tenant) is not None
                                or locked_bot.status == BOT_STATUS_INVALID_TOKEN
                            ):
                                raise _Ineligible
                            await self._require_lease(slot)

                        pool = ProxyPoolService(session, self._proxy, self._health)
                        await pool.assign_proxy_in_transaction(bot_id, before_bind=before_bind)
                        await self._require_lease(slot)
                except (EntityNotFoundError, _Ineligible):
                    # A concurrent soft-delete can win after the initial read.
                    return
                except NoProxyAvailableError:
                    recorded = await self._write_status(
                        slot, BOT_STATUS_ERROR, "no_proxy_available"
                    )
                    if recorded and self._on_no_proxy is not None:
                        self._on_no_proxy()
                    return
            state = await self._read(bot_id)
            if state is None or self._closing:
                return
            bot, reason, proxy = state
            if reason is not None or bot.status == BOT_STATUS_INVALID_TOKEN:
                return
            if self._proxy.tenants_use_proxy and (
                proxy is None or proxy.status != PROXY_STATUS_ACTIVE
            ):
                return
            slot.token_hash = bot.token_hash
            slot.restart_revision = bot.restart_revision
            slot.proxy_id = proxy.id if proxy is not None else None
            await self._require_lease(slot)
            if self._closing:
                return

            async def status_sink(status: str, error: str | None) -> bool:
                return await self._write_status(slot, status, error)

            async def proxy_failure(_: UUID, proxy_id: UUID, exc: Exception) -> None:
                async def fence() -> None:
                    if self._closing:
                        raise _LeaseLost
                    await self._require_lease(slot)

                try:
                    async with self._sessions() as session:
                        pool = ProxyPoolService(session, self._proxy, self._health)
                        verdict = await pool.report_failure(
                            proxy_id, exc, fence=fence, lease=slot.lease
                        )
                except _LeaseLost:
                    return
                if verdict.released_bot_ids:
                    # Never await a stop from inside the polling task it stops.
                    self._wake.set()

            if proxy is None:
                # A previous proxy-enabled deployment can leave a sticky DB binding.
                # Do not pass it to the direct-polling runtime.
                bot.proxy_id = None

            def can_poll() -> bool:
                return (
                    not self._closing
                    and not slot.lost
                    and asyncio.get_running_loop().time() < slot.deadline
                )

            runner = await self._supervisor.start(
                bot,
                proxy,
                token=bot.encrypted_token,
                dispatcher=self._dispatcher,
                status_sink=status_sink,
                health=self._health,
                on_proxy_failure=proxy_failure,
                can_poll=can_poll,
                lease=slot.lease,
            )
            slot.runner = runner
            runner.start()
            slot.completion = runner.completion
        except _LeaseLost:
            return
        finally:
            if slot.runner is None:
                await self._retire(bot_id, slot)

    async def _write_status(self, slot: _Slot, status: str, error: str | None) -> bool:
        if slot.lost:
            return False
        try:
            async with self._sessions.begin() as session:
                repo = BotRepository(session)
                # Serializes status writers across generations. Check ownership
                # after the lock (not before a potentially long DB wait).
                bot = await repo.get(slot.lease.bot_id, include_deleted=True, for_update=True)
                if (
                    bot is None
                    or bot.status == BOT_STATUS_INVALID_TOKEN
                    or (
                        slot.token_hash is not None
                        and (
                            bot.token_hash != slot.token_hash
                            or bot.restart_revision != slot.restart_revision
                        )
                    )
                ):
                    return False
                tenant = await session.get(Tenant, bot.tenant_id)
                reason = self._reason(bot, tenant)
                if reason is not None:
                    if status != BOT_STATUS_STOPPED or bot.status == BOT_STATUS_STOPPED:
                        return False
                    error = reason
                elif (
                    status != BOT_STATUS_STOPPED
                    and slot.runner is not None
                    and self._proxy.tenants_use_proxy
                    and bot.proxy_id != slot.proxy_id
                ):
                    return False
                await self._require_lease(slot)
                await repo.set_status(bot, status, error=error)
                await self._require_lease(slot)
        except _LeaseLost:
            return False
        return True

    async def _retire(self, bot_id: UUID, slot: _Slot) -> None:
        try:
            if slot.runner is not None:
                await slot.runner.stop()
        finally:
            if slot.completion is None or slot.completion.done():
                try:
                    async with asyncio.timeout(min(self._ttl / 4, 1.0)):
                        await slot.lease.release()
                finally:
                    self._slots.pop(bot_id, None)

    def close_admission(self) -> None:
        self._closing = True
        self._wake.set()
        for slot in self._slots.values():
            if slot.runner is not None:
                slot.runner.request_stop()

    async def shutdown(self) -> None:
        self.close_admission()
        async with self._lock:
            results = await asyncio.gather(
                *(self._retire(bot_id, slot) for bot_id, slot in tuple(self._slots.items())),
                return_exceptions=True,
            )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("bot worker shutdown incomplete", errors)
