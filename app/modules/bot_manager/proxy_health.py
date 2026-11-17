"""Proxy health: Redis marks of the strike window and the proxy handshake probe.

Keys (TTL = ``PROXY_STRIKE_WINDOW_SECONDS``): ``proxy:ok:{id}`` - last successful
``getUpdates`` through the proxy (the poller calls :meth:`ProxyHealthTracker.mark_ok`,
throttled locally), ``proxy:fail:{id}`` - the poller reached ``PROXY_FAILURE_THRESHOLD``
errors (:meth:`ProxyHealthTracker.mark_fail`), ``proxy:strike:{id}`` - dedup of strikes,
``proxy:outage`` - suspected Telegram outage shared by all processes.
A proxy is "ok" when it has ``proxy:ok`` and no newer ``proxy:fail``.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import UUID

import structlog

from app.modules.bot_manager.metrics import (
    OUTAGE_KEY,
    OUTAGE_POLL_INTERVAL_SECONDS,
    LabeledCounter,
    OutageState,
    proxy_probe_total,
)
from app.shared_kernel.infrastructure.generational_cache import REDIS_ERRORS
from app.shared_kernel.infrastructure.redis_scripts import compare_and_delete

__all__ = [
    "OUTAGE_KEY",
    "TELEGRAM_API_HOST",
    "HandshakeResult",
    "OutageMonitor",
    "PoolHealth",
    "ProxyHealthTracker",
    "fail_key",
    "ok_key",
    "probe_handshake",
    "strike_key",
]

TELEGRAM_API_HOST: Final = "api.telegram.org"
DEFAULT_OK_THROTTLE_SECONDS: Final = 30.0
_log = structlog.get_logger(__name__)

#: Timeout of one ``EXISTS proxy:outage`` of :class:`OutageMonitor`, seconds.
OUTAGE_CHECK_TIMEOUT_SECONDS: Final = 2.0

ProbeErrorKind = Literal["proxy_dead", "telegram_blocked"]


def ok_key(proxy_id: UUID) -> str:
    return f"proxy:ok:{proxy_id}"


def fail_key(proxy_id: UUID) -> str:
    return f"proxy:fail:{proxy_id}"


def strike_key(proxy_id: UUID) -> str:
    return f"proxy:strike:{proxy_id}"


def _ts(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.decode() if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class PoolHealth:
    """Health of a set of proxies in the current window."""

    ok: int
    failed: int
    total: int


class ProxyHealthTracker:
    """Redis marks of proxy success/failure and of a Telegram outage."""

    def __init__(
        self,
        redis: Any,
        *,
        window_seconds: int,
        ok_throttle_seconds: float = DEFAULT_OK_THROTTLE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._window = window_seconds
        self._throttle = ok_throttle_seconds
        self._clock = clock
        self._last_ok: dict[UUID, float] = {}

    async def mark_ok(self, proxy_id: UUID) -> bool:
        """Mark a successful request and clear the outage key (one ``SET`` + one ``DEL``
        per throttle period); ``False`` - throttled or Redis failed (logged)."""
        now = self._clock()
        last = self._last_ok.get(proxy_id)
        if last is not None and now - last < self._throttle:
            return False
        try:
            await self._redis.set(ok_key(proxy_id), f"{now:.6f}", ex=self._window)
            if await self._redis.delete(OUTAGE_KEY):
                _log.info("telegram_outage_cleared", proxy_id=str(proxy_id))
        except REDIS_ERRORS as error:
            _log.warning("proxy_mark_ok_failed", proxy_id=str(proxy_id), error=type(error).__name__)
            return False
        self._last_ok[proxy_id] = now
        return True

    async def mark_fail(self, proxy_id: UUID) -> bool:
        """Mark a failed proxy (the next ``mark_ok`` is not throttled)."""
        now = self._clock()
        self._last_ok.pop(proxy_id, None)
        try:
            await self._redis.set(fail_key(proxy_id), f"{now:.6f}", ex=self._window)
        except REDIS_ERRORS as error:
            _log.warning(
                "proxy_mark_fail_failed", proxy_id=str(proxy_id), error=type(error).__name__
            )
            return False
        return True

    async def pool_health(self, proxy_ids: Sequence[UUID]) -> PoolHealth:
        """One ``MGET`` over the marks of ``proxy_ids``; Redis errors propagate."""
        ids = list(proxy_ids)
        if not ids:
            return PoolHealth(ok=0, failed=0, total=0)
        raw = await self._redis.mget([ok_key(i) for i in ids] + [fail_key(i) for i in ids])
        ok = failed = 0
        for index in range(len(ids)):
            ok_ts, fail_ts = _ts(raw[index]), _ts(raw[len(ids) + index])
            if ok_ts is not None and (fail_ts is None or ok_ts > fail_ts):
                ok += 1
            elif fail_ts is not None:
                failed += 1
        return PoolHealth(ok=ok, failed=failed, total=len(ids))

    async def set_outage(self) -> None:
        """``SET proxy:outage <ts> EX window``; Redis errors propagate."""
        await self._redis.set(OUTAGE_KEY, f"{self._clock():.6f}", ex=self._window)

    async def outage_active(self) -> bool:
        return bool(await self._redis.exists(OUTAGE_KEY))

    async def try_acquire_strike(self, proxy_id: UUID) -> str | None:
        """``SET proxy:strike:{id} <token> NX EX window``: the owner token of the first
        strike in the window, ``None`` - already taken. Redis errors propagate."""
        token = uuid.uuid4().hex
        acquired = await self._redis.set(strike_key(proxy_id), token, nx=True, ex=self._window)
        return token if acquired else None

    async def release_strike(self, proxy_id: UUID, token: str) -> bool:
        """Best effort: free the dedup key of a strike whose transaction failed - only while
        it still holds ``token`` (a key re-taken after expiry is never removed)."""
        try:
            return await compare_and_delete(self._redis, strike_key(proxy_id), token)
        except REDIS_ERRORS as error:
            _log.warning(
                "proxy_strike_release_failed", proxy_id=str(proxy_id), error=type(error).__name__
            )
            return False


class OutageMonitor:
    """Background poll of ``EXISTS proxy:outage`` into :class:`OutageState` (``master_bot``).

    Every ``interval_seconds`` one async ``EXISTS`` bounded by ``timeout_seconds``; a failed
    or timed-out check keeps the old value, which goes stale after ``2 x interval`` and the
    ``telegram_outage`` series disappears. The ``/metrics`` handler never waits for Redis.
    """

    def __init__(
        self,
        redis: Any,
        state: OutageState,
        *,
        interval_seconds: float = OUTAGE_POLL_INTERVAL_SECONDS,
        timeout_seconds: float = OUTAGE_CHECK_TIMEOUT_SECONDS,
    ) -> None:
        self._redis = redis
        self._state = state
        self._interval = interval_seconds
        self._timeout = timeout_seconds

    async def check_once(self) -> bool:
        """One bounded check; ``False`` - Redis failed or timed out (state unchanged)."""
        try:
            async with asyncio.timeout(self._timeout):
                active = bool(await self._redis.exists(OUTAGE_KEY))
        except REDIS_ERRORS as error:
            _log.warning("telegram_outage_check_failed", error=type(error).__name__)
            return False
        self._state.update(active)
        return True

    async def run(self) -> None:
        while True:
            await self.check_once()
            await asyncio.sleep(self._interval)

    @asynccontextmanager
    async def running(self) -> AsyncIterator[asyncio.Task[None]]:
        """Run the poll loop for the duration of the block; cancelled and awaited on exit."""
        task = asyncio.create_task(self.run(), name="telegram-outage-monitor")
        try:
            yield task
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@dataclass(frozen=True, slots=True)
class HandshakeResult:
    """``proxy_ok`` - the tunnel through the proxy is open; ``telegram_ok`` - TLS/HTTP to
    Telegram answered. ``detail`` is an exception class name (never the URL)."""

    proxy_ok: bool
    telegram_ok: bool
    error_kind: ProbeErrorKind | None
    detail: str | None = None

    @property
    def result(self) -> str:
        return self.error_kind or "ok"


async def probe_handshake(
    proxy_url: str,
    timeout_seconds: float = 10.0,
    *,
    host: str = TELEGRAM_API_HOST,
    port: int = 443,
    tls: bool = True,
    counter: LabeledCounter = proxy_probe_total,
) -> HandshakeResult:
    """Open a tunnel to ``host:port`` through the proxy and send ``HEAD /``.

    Failure while connecting/authenticating to the proxy -> ``proxy_dead``; the tunnel is
    open but TLS/HTTP to Telegram fails -> ``telegram_blocked``. Counted in
    ``proxy_probe_total{result}``. ``timeout_seconds`` applies to every network step.
    """
    result = await _probe(proxy_url, timeout_seconds, host=host, port=port, tls=tls)
    counter.inc(result.result)
    return result


async def _probe(
    proxy_url: str, timeout_seconds: float, *, host: str, port: int, tls: bool
) -> HandshakeResult:
    from python_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError
    from python_socks.async_.asyncio import Proxy as SocksProxy

    try:
        proxy = SocksProxy.from_url(proxy_url, rdns=True)
        sock = await proxy.connect(dest_host=host, dest_port=port, timeout=timeout_seconds)
    except (
        ProxyError,
        ProxyTimeoutError,
        ProxyConnectionError,
        OSError,
        TimeoutError,
        ValueError,
    ) as error:
        return HandshakeResult(False, False, "proxy_dead", type(error).__name__)
    writer: asyncio.StreamWriter | None = None
    try:
        reader, stream = await asyncio.wait_for(
            asyncio.open_connection(
                sock=sock,
                ssl=ssl.create_default_context() if tls else None,
                server_hostname=host if tls else None,
            ),
            timeout_seconds,
        )
        writer = stream
        stream.write(f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        await asyncio.wait_for(stream.drain(), timeout_seconds)
        line = await asyncio.wait_for(reader.readline(), timeout_seconds)
    except (OSError, TimeoutError, asyncio.IncompleteReadError) as error:
        return HandshakeResult(True, False, "telegram_blocked", type(error).__name__)
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        else:
            sock.close()
    if not line.startswith(b"HTTP/"):
        return HandshakeResult(True, False, "telegram_blocked", "no_http_response")
    return HandshakeResult(True, True, None)
