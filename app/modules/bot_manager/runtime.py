"""Isolated long-polling runtime for shop bots (task-10)."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import ParamSpec, Protocol
from uuid import UUID

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError
from python_socks import ProxyConnectionError as RawProxyConnectionError
from python_socks import ProxyError as RawProxyError
from python_socks import ProxyTimeoutError as RawProxyTimeoutError

from app.core.config import Settings
from app.core.observability.metrics import AppMetrics
from app.modules.bot_manager.lease import BotLease
from app.modules.bot_manager.models import (
    BOT_STATUS_ERROR,
    BOT_STATUS_INVALID_TOKEN,
    BOT_STATUS_RUNNING,
    BOT_STATUS_STARTING,
    BOT_STATUS_STOPPED,
    Proxy,
)
from app.modules.bot_manager.models import Bot as BotRecord
from app.modules.bot_manager.proxy_health import ProxyHealthTracker, probe_handshake
from app.modules.bot_manager.sessions import build_bot_session

log = logging.getLogger(__name__)
StatusSink = Callable[[str, str | None], Awaitable[bool | None]]
ProxyFailureCallback = Callable[[UUID, UUID, Exception], Awaitable[None]]
_P = ParamSpec("_P")
_NETWORK_ERRORS = (
    TelegramNetworkError,
    TimeoutError,
    ConnectionError,
    ProxyError,
    ProxyConnectionError,
    ProxyTimeoutError,
    RawProxyError,
    RawProxyConnectionError,
    RawProxyTimeoutError,
)


class FinalStatusWriteError(RuntimeError):
    """A final status could not be persisted; contains no sink exception text."""


class MetricSink(Protocol):
    def set(self, value: float, /) -> None: ...


class BotRunner:
    def __init__(
        self,
        bot_id: UUID,
        telegram_bot_id: int,
        tenant_id: UUID,
        token: str,
        proxy_id: UUID | None,
        proxy_url: str | None,
        dispatcher: Dispatcher,
        status_sink: StatusSink,
        health: ProxyHealthTracker | None = None,
        on_proxy_failure: ProxyFailureCallback | None = None,
        *,
        failure_threshold: int = 5,
        session: BaseSession | None = None,
        metrics: AppMetrics | None = None,
        can_poll: Callable[[], bool] | None = None,
        lease: BotLease | None = None,
    ) -> None:
        self.bot_id, self.telegram_bot_id, self.tenant_id = bot_id, telegram_bot_id, tenant_id
        self._token, self.proxy_id, self.proxy_url = token, proxy_id, proxy_url
        self.dispatcher, self.status_sink, self.health = dispatcher, status_sink, health
        self.on_proxy_failure, self.failure_threshold = on_proxy_failure, failure_threshold
        self._session, self._metrics = session, metrics
        self._can_poll = can_poll or (lambda: True)
        self.polling_succeeded = False
        self._lease = lease
        self._startup_accepted = False
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._cleanup: asyncio.Task[None] | None = None
        self._completion: asyncio.Future[None] | None = None
        self._terminal_status: tuple[str, str] | None = None

    async def _call(
        self,
        operation: str,
        callback: Callable[_P, Awaitable[object]],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> None:
        try:
            await callback(*args, **kwargs)
        except Exception as exc:
            log.warning("bot %s failed: %s", operation, type(exc).__name__)

    async def _status(self, status: str, error: str | None = None) -> None:
        await self._call("status sink", self.status_sink, status, error)

    async def _poll(self) -> None:
        try:
            accepted = await self.status_sink(BOT_STATUS_STARTING, None)
        except Exception as exc:
            log.warning("bot status sink failed: %s", type(exc).__name__)
            if self._lease is not None:
                return
            accepted = None
        if accepted is False:
            return
        self._startup_accepted = True
        try:
            if self._session is None:
                self._session = build_bot_session(self.proxy_url)
            bot = Bot(self._token, session=self._session)
        except Exception as exc:
            self._terminal_status = (BOT_STATUS_ERROR, type(exc).__name__)
            return

        offset: int | None = None
        retries = proxy_failures = 0
        running = False
        while not self._stop.is_set() and self._can_poll():
            try:
                updates = await bot.get_updates(
                    offset=offset, timeout=30, request_timeout=40, allowed_updates=[]
                )
            except TelegramUnauthorizedError as exc:
                if self._stop.is_set() or not self._can_poll():
                    return
                self._terminal_status = (BOT_STATUS_INVALID_TOKEN, type(exc).__name__)
                return
            except TelegramRetryAfter as exc:
                if self._stop.is_set() or not self._can_poll():
                    return
                proxy_failures = 0
                await self._sleep(float(exc.retry_after))
                continue
            except Exception as exc:
                if self._stop.is_set() or not self._can_poll():
                    return
                if self.proxy_id is not None and isinstance(exc, _NETWORK_ERRORS):
                    proxy_failures += 1
                    if proxy_failures == self.failure_threshold:
                        await self._report_proxy_failure(self.proxy_id, exc)
                else:
                    proxy_failures = 0
                    log.warning("bot polling failed: %s", type(exc).__name__)
                ceiling = min(60.0, float(2**retries))
                retries = min(retries + 1, 6)
                await self._sleep(random.uniform(ceiling / 2, ceiling))  # noqa: S311
                continue

            if self._stop.is_set() or not self._can_poll():
                return
            self.polling_succeeded = True
            retries = proxy_failures = 0
            if self.proxy_id is not None and self.health is not None:
                if self._lease is None:
                    await self._call("proxy mark_ok", self.health.mark_ok, self.proxy_id)
                else:
                    await self._call(
                        "proxy mark_ok", self.health.mark_ok, self.proxy_id, lease=self._lease
                    )
            if not running:
                await self._status(BOT_STATUS_RUNNING)
                running = True
            for update in updates:
                if self._stop.is_set() or not self._can_poll():
                    break
                offset = update.update_id + 1
                try:
                    await self.dispatcher.feed_update(
                        bot,
                        update,
                        bot_id=self.bot_id,
                        telegram_bot_id=self.telegram_bot_id,
                        tenant_id=self.tenant_id,
                    )
                except Exception as exc:
                    if self._metrics is not None:
                        self._metrics.app_errors_total.labels(component="bot_worker").inc()
                    log.error("bot update handler failed: %s", type(exc).__name__)

    async def _report_proxy_failure(self, proxy_id: UUID, exc: Exception) -> None:
        if self._stop.is_set() or not self._can_poll():
            return
        if self.health is not None:
            if self._lease is None:
                await self._call("proxy mark_fail", self.health.mark_fail, proxy_id)
            else:
                await self._call(
                    "proxy mark_fail", self.health.mark_fail, proxy_id, lease=self._lease
                )
        if self.proxy_url and not self._stop.is_set() and self._can_poll():
            await self._call("proxy probe", probe_handshake, self.proxy_url)
        if self.on_proxy_failure is not None and not self._stop.is_set() and self._can_poll():
            await self._call(
                "proxy failure callback", self.on_proxy_failure, self.bot_id, proxy_id, exc
            )

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _write_final_status(self, status: str, error: str | None) -> None:
        try:
            await self.status_sink(status, error)
        except Exception as exc:
            error_type = type(exc).__name__
        else:
            return
        failure = FinalStatusWriteError(
            f"Bot {self.bot_id}: failed to persist {status} ({error_type})"
        )
        log.error("%s", failure)
        raise failure from None

    async def _finalize(self) -> None:
        # The runner can be cancelled while a handler is still unwinding.
        if self._poll_task is not None:
            await asyncio.wait((self._poll_task,))
        # Only cleanup publishes terminal outcomes; stopping polling cannot interrupt them.
        close_cancelled: asyncio.CancelledError | None = None
        try:
            if self._terminal_status is not None:
                await self._write_final_status(*self._terminal_status)
        finally:
            if self._session is not None:
                try:
                    await self._call("session close", self._session.close)
                except asyncio.CancelledError as exc:
                    # Do not mask a status failure already propagating through finally.
                    close_cancelled = exc
        if self._startup_accepted and self._stop.is_set() and self._terminal_status is None:
            await self._write_final_status(BOT_STATUS_STOPPED, None)
        if close_cancelled is not None:
            raise close_cancelled

    def _ensure_cleanup(self) -> asyncio.Task[None]:
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._finalize(), name=f"bot-cleanup-{self.bot_id}")
        return self._cleanup

    async def _run(self) -> None:
        try:
            if not self._stop.is_set():
                self._poll_task = asyncio.create_task(self._poll(), name=f"bot-poll-{self.bot_id}")
                try:
                    await asyncio.shield(self._poll_task)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if not self._stop.is_set() or (current is not None and current.cancelling()):
                        raise
        except asyncio.CancelledError:
            # Set the flag before cancelling polling: a handler may swallow cancellation.
            self._request_stop()
            raise
        finally:
            cleanup = self._ensure_cleanup()
            # Do not mask an already propagating cancellation with a cleanup error.
            await asyncio.wait((cleanup,))
        cleanup.result()

    async def run(self) -> None:
        await self.start()

    def request_stop(self) -> None:
        """Stop admission to polling immediately, without waiting for cleanup."""
        self._request_stop()

    def _request_stop(self) -> None:
        if not self._stop.is_set():
            self._stop.set()
            if self._poll_task is not None:
                self._poll_task.cancel()

    async def close(self) -> None:
        await self.stop()

    async def stop(self, timeout: float = 10.0) -> None:  # noqa: ASYNC109
        """Request stop and wait for cleanup; timeout leaves the live slot registered."""
        self._request_stop()
        signal = self._completion_signal
        _, pending = await asyncio.wait((signal,), timeout=timeout)
        if pending:
            raise TimeoutError("Bot runner is still stopping")
        signal.result()

    def start(self) -> asyncio.Task[None]:
        if self._task is None:
            self._completion = asyncio.get_running_loop().create_future()
            self._task = asyncio.create_task(self._run(), name=f"bot-runner-{self.bot_id}")
            self._task.add_done_callback(self._on_exit)
        return self._task

    @property
    def completion(self) -> asyncio.Future[None]:
        """Return a separate waiter; cancelling it does not complete the runner."""
        waiter = asyncio.shield(self._completion_signal)
        waiter.add_done_callback(self._observe_completion)
        return waiter

    @staticmethod
    def _observe_completion(waiter: asyncio.Future[None]) -> None:
        # A caller may inspect .done() without awaiting this shielded copy.
        if not waiter.cancelled():
            waiter.exception()

    @property
    def _completion_signal(self) -> asyncio.Future[None]:
        self.start()
        if self._completion is None:
            raise RuntimeError("Runner completion was not initialized")
        return self._completion

    def _on_exit(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("bot runner exited: %s", type(error).__name__)
        if task.cancelled():
            self._stop.set()
        cleanup = self._ensure_cleanup()  # Also runs if cancelled before _run entered.
        if cleanup.done():
            self._on_cleanup_done(cleanup)
        else:
            cleanup.add_done_callback(self._on_cleanup_done)

    def _on_cleanup_done(self, cleanup: asyncio.Task[None]) -> None:
        error = (
            RuntimeError("Bot cleanup was cancelled")
            if cleanup.cancelled()
            else cleanup.exception()
        )
        if error is not None:
            log.error("bot cleanup exited: %s", type(error).__name__)
        signal = self._completion_signal
        if signal.done():
            return
        if error is None:
            signal.set_result(None)
        else:
            signal.set_exception(error)
            self._observe_completion(signal)


class BotSupervisor:
    def __init__(
        self,
        *,
        metrics: AppMetrics,
        max_bots: int = 100,
        failure_threshold: int = 5,
        active_metric: MetricSink | None = None,
    ) -> None:
        if active_metric is not None and active_metric is not metrics.active_bots:
            raise ValueError("active_metric must be the gauge from metrics")
        self.max_bots, self.failure_threshold = max_bots, failure_threshold
        self._metrics = metrics
        self._runners: dict[UUID, BotRunner] = {}
        self._set_metric()

    @classmethod
    def from_settings(cls, settings: Settings, *, metrics: AppMetrics) -> BotSupervisor:
        return cls(
            metrics=metrics,
            max_bots=settings.bot_worker.max_bots,
            failure_threshold=settings.proxy.proxy_failure_threshold,
        )

    async def start(
        self,
        bot: BotRecord,
        proxy: Proxy | str | None = None,
        *,
        token: str | None = None,
        dispatcher: Dispatcher,
        status_sink: StatusSink,
        health: ProxyHealthTracker | None = None,
        on_proxy_failure: ProxyFailureCallback | None = None,
        failure_threshold: int | None = None,
        session: BaseSession | None = None,
        can_poll: Callable[[], bool] | None = None,
        lease: BotLease | None = None,
    ) -> BotRunner:
        if (existing := self._runners.get(bot.id)) is not None:
            if not existing._completion_signal.done():
                return existing
            self._finished(existing)
        if len(self.running_ids()) >= self.max_bots:
            raise RuntimeError("BOT_WORKER_MAX_BOTS reached")
        if not token:
            raise TypeError("BotSupervisor.start requires decrypted token= input")
        runner = BotRunner(
            bot.id,
            bot.telegram_bot_id,
            bot.tenant_id,
            token,
            bot.proxy_id,
            proxy.url if isinstance(proxy, Proxy) else proxy,
            dispatcher,
            status_sink,
            health,
            on_proxy_failure,
            failure_threshold=self.failure_threshold
            if failure_threshold is None
            else failure_threshold,
            session=session,
            metrics=self._metrics,
            can_poll=can_poll,
            lease=lease,
        )
        self._runners[bot.id] = runner
        runner._completion_signal.add_done_callback(lambda _: self._finished(runner))
        self._set_metric()
        return runner

    def _finished(self, runner: BotRunner) -> None:
        if self._runners.get(runner.bot_id) is runner:
            del self._runners[runner.bot_id]
            self._set_metric()

    async def stop(self, bot_id: UUID, timeout: float = 10.0) -> None:  # noqa: ASYNC109
        if (runner := self._runners.get(bot_id)) is not None:
            try:
                await runner.stop(timeout)
            finally:
                if runner._completion_signal.done():
                    self._finished(runner)

    async def stop_all(self, timeout: float = 10.0) -> None:  # noqa: ASYNC109
        snapshot = {runner._completion_signal: runner for runner in self._runners.values()}
        if not snapshot:
            return
        for runner in snapshot.values():
            runner._request_stop()
        done, pending = await asyncio.wait(snapshot, timeout=timeout)
        errors: list[Exception] = []
        for completed in done:
            self._finished(snapshot[completed])
            if isinstance(error := completed.exception(), Exception):
                errors.append(error)
        if pending:
            timeout_error = TimeoutError("Bot runners are still stopping")
            if not errors:
                raise timeout_error
            errors.append(timeout_error)
        if errors:
            raise ExceptionGroup("Bot runners did not stop successfully", errors)

    def running_ids(self) -> set[UUID]:
        return {
            bot_id
            for bot_id, runner in self._runners.items()
            if not runner._completion_signal.done()
        }

    def _set_metric(self) -> None:
        self._metrics.active_bots.set(len(self.running_ids()))


__all__ = ["BotRunner", "BotSupervisor", "FinalStatusWriteError", "MetricSink"]
