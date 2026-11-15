"""Proxy pool: URL masking, settings, sessions and the handshake probe (fake SOCKS5)."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import ProxySettings, Settings
from app.core.observability.metrics import (
    build_metrics,
    register_component_metrics,
    render_metrics,
)
from app.core.proxy_url import validate_proxy_url
from app.modules.bot_manager.metrics import FAILURE_VERDICTS, PROBE_RESULTS, LabeledCounter
from app.modules.bot_manager.models import Proxy
from app.modules.bot_manager.proxy_health import probe_handshake
from app.modules.bot_manager.sessions import build_bot_session, mask_proxy_url
from app.modules.logging.metrics import LogsDroppedCounter

pytestmark = pytest.mark.req("R-II.14")


def _proxy_settings(**values: Any) -> ProxySettings:
    """Validate a section without env/dotenv sources (``model_validate`` skips them)."""
    return ProxySettings.model_validate(values)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("socks5://user:secret@10.0.0.1:1080", "socks5://10.0.0.1:1080"),
        ("http://u:p@proxy.example:3128/path?x=1", "http://proxy.example:3128"),
        ("socks4://[::1]:9050", "socks4://[::1]:9050"),
        ("garbage", "?://?"),
        ("login:pass@host:1080", "?://?"),
        ("ftp://u:secret@h:21", "?://h:21"),
        ("socks5://u:p@h:99999", "<invalid proxy url>"),
    ],
)
def test_mask_proxy_url_hides_credentials(url: str, expected: str) -> None:
    masked = mask_proxy_url(url)
    assert masked == expected
    assert "secret" not in masked
    assert "u:p" not in masked


def test_proxy_repr_has_no_url() -> None:
    proxy = Proxy(url="socks5://u:secret@h:1", url_hash="abc", status="active", strike_count=0)
    assert "secret" not in repr(proxy)
    assert "abc" not in repr(proxy)


def test_proxy_settings_defaults() -> None:
    s = _proxy_settings()
    assert (s.master_bot_use_proxy, s.master_bot_proxy_url, s.tenants_use_proxy) == (
        False,
        None,
        False,
    )
    assert (
        s.proxy_max_bots_per_ip,
        s.proxy_strikes_to_quarantine,
        s.proxy_quarantine_days,
        s.proxy_strike_window_seconds,
        s.proxy_failure_threshold,
        s.proxy_outage_ratio,
    ) == (20, 2, 7, 3600, 5, 0.5)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("proxy_max_bots_per_ip", 0),
        ("proxy_strikes_to_quarantine", 0),
        ("proxy_quarantine_days", 0),
        ("proxy_strike_window_seconds", 0),
        ("proxy_failure_threshold", 0),
        ("proxy_outage_ratio", 0),
        ("proxy_outage_ratio", 1.1),
    ],
)
def test_proxy_settings_bounds(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        _proxy_settings(**{field: value})


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("SOCKS5://User:Pass@Host.Example:1080", "socks5://User:Pass@host.example:1080"),
        (" http://10.0.0.1:3128/path ", "http://10.0.0.1:3128"),
        ("socks4://[::1]:9050", "socks4://[::1]:9050"),
    ],
)
def test_validate_proxy_url_normalizes(url: str, expected: str) -> None:
    assert validate_proxy_url(url) == expected


@pytest.mark.parametrize("url", ["socks5://u:secret@h", "login:secret@host:1080", "https://h:1"])
def test_validate_proxy_url_rejects_without_secret(url: str) -> None:
    with pytest.raises(ValueError) as info:  # noqa: PT011 - message checked below
        validate_proxy_url(url)
    assert "secret" not in str(info.value)
    assert "login" not in str(info.value)


def test_master_proxy_url_required_when_enabled() -> None:
    with pytest.raises(ValidationError, match="MASTER_BOT_PROXY_URL is required"):
        _proxy_settings(master_bot_use_proxy=True, master_bot_proxy_url="")


@pytest.mark.parametrize(
    "url", ["ftp://user:secret@h:21", "https://user:secret@h:443", "socks5://user:secret@h"]
)
def test_master_proxy_url_validator_hides_secret(url: str) -> None:
    with pytest.raises(ValidationError) as info:
        _proxy_settings(master_bot_use_proxy=True, master_bot_proxy_url=url)
    assert "secret" not in str(info.value)


def test_master_proxy_url_accepted() -> None:
    s = _proxy_settings(
        master_bot_use_proxy=True,
        master_bot_proxy_url="socks5://u:secret@10.0.0.1:1080",
    )
    assert s.master_bot_proxy_url is not None
    assert "secret" not in repr(s)
    assert s.master_bot_proxy_url.get_secret_value().endswith("@10.0.0.1:1080")


def test_proxy_section_reads_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("PROXY_MAX_BOTS_PER_IP", "PROXY_OUTAGE_RATIO"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("PROXY_MAX_BOTS_PER_IP=7\nPROXY_OUTAGE_RATIO=0.25\n", encoding="utf-8")
    settings = Settings(_env_file=str(env))
    assert settings.proxy.proxy_max_bots_per_ip == 7
    assert settings.proxy.proxy_outage_ratio == 0.25


async def test_build_bot_session_socks5() -> None:
    session = build_bot_session("socks5://user:pass@127.0.0.1:1080")
    try:
        assert session.proxy is not None
    finally:
        await session.close()
    direct = build_bot_session(None)
    assert direct.proxy is None
    await direct.close()


def test_build_bot_session_rejects_bad_scheme() -> None:
    with pytest.raises(ValueError, match="Unsupported proxy scheme") as info:
        build_bot_session("ftp://user:secret@h:21")
    assert "secret" not in str(info.value)


# --- fake SOCKS5 server ------------------------------------------------------------------

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _read_connect(reader: asyncio.StreamReader) -> None:
    head = await reader.readexactly(4)
    atyp = head[3]
    if atyp == 3:
        await reader.readexactly((await reader.readexactly(1))[0])
    else:
        await reader.readexactly(4 if atyp == 1 else 16)
    await reader.readexactly(2)


def _socks(mode: str) -> Handler:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            _, count = await reader.readexactly(2)
            await reader.readexactly(count)
            if mode == "auth_reject":
                writer.write(b"\x05\x02")
                await writer.drain()
                _, user_len = await reader.readexactly(2)
                await reader.readexactly(user_len)
                await reader.readexactly((await reader.readexactly(1))[0])
                writer.write(b"\x01\x01")
                await writer.drain()
                return
            writer.write(b"\x05\x00")
            await reader.readexactly(0)
            await writer.drain()
            await _read_connect(reader)
            writer.write(b"\x05\x00\x00\x01" + bytes(6))
            await writer.drain()
            if mode == "ok":
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 302 Found\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
            # "silent": the tunnel is open, the target never answers -> close.
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    return handle


@asynccontextmanager
async def _server(mode: str) -> AsyncIterator[int]:
    server = await asyncio.start_server(_socks(mode), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


async def test_probe_proxy_auth_rejected_is_proxy_dead() -> None:
    counter = LabeledCounter(PROBE_RESULTS)
    async with _server("auth_reject") as port:
        result = await probe_handshake(
            f"socks5://user:bad@127.0.0.1:{port}", 2.0, tls=False, counter=counter
        )
    assert (result.proxy_ok, result.telegram_ok, result.error_kind) == (False, False, "proxy_dead")
    assert counter.snapshot()["proxy_dead"] == 1


async def test_probe_proxy_unreachable_is_proxy_dead() -> None:
    # Bound but not listening (kept open): the port can not be reused by anyone else and a
    # connect is refused deterministically (Linux/macOS), no race with a closed server.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        result = await probe_handshake(f"socks5://127.0.0.1:{port}", 2.0, tls=False)
    assert (result.proxy_ok, result.error_kind) == (False, "proxy_dead")


@pytest.mark.parametrize("tls", [False, True])
async def test_probe_tunnel_without_telegram_is_blocked(tls: bool) -> None:
    async with _server("silent") as port:
        result = await probe_handshake(f"socks5://127.0.0.1:{port}", 2.0, tls=tls)
    assert (result.proxy_ok, result.telegram_ok, result.error_kind) == (
        True,
        False,
        "telegram_blocked",
    )


async def test_probe_ok() -> None:
    counter = LabeledCounter(PROBE_RESULTS)
    async with _server("ok") as port:
        result = await probe_handshake(
            f"socks5://127.0.0.1:{port}", 2.0, tls=False, counter=counter
        )
    assert (result.proxy_ok, result.telegram_ok, result.error_kind) == (True, True, None)
    assert counter.snapshot() == {"ok": 1, "proxy_dead": 0, "telegram_blocked": 0}


# --- metrics by process role ---------------------------------------------------------------


class _FakeSyncRedis:
    def __init__(self, value: int | Exception) -> None:
        self.value = value
        self.keys: list[str] = []

    def exists(self, key: str) -> int:
        self.keys.append(key)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def _render(component: str, redis: Any = None) -> str:
    metrics = build_metrics(LogsDroppedCounter())
    register_component_metrics(metrics, component=component, outage_redis=redis)
    return render_metrics(metrics.registry).decode()


def test_worker_exports_proxy_counters_with_zeros() -> None:
    text = _render("bot_worker")
    for verdict in FAILURE_VERDICTS:
        assert f'proxy_failure_verdicts_total{{verdict="{verdict}"}}' in text
    for result in PROBE_RESULTS:
        assert f'proxy_probe_total{{result="{result}"}}' in text
    assert "telegram_outage" not in text


@pytest.mark.parametrize(
    ("value", "expected"), [(1, "telegram_outage 1.0"), (0, "telegram_outage 0.0")]
)
def test_master_exports_outage_gauge_from_redis(value: int, expected: str) -> None:
    redis = _FakeSyncRedis(value)
    text = _render("master_bot", redis)
    assert expected in text
    assert redis.keys == ["proxy:outage"]
    assert "proxy_failure_verdicts_total" not in text


def test_master_outage_gauge_omitted_when_redis_down() -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    text = _render("master_bot", _FakeSyncRedis(RedisConnectionError("down")))
    assert "telegram_outage" not in text
    assert "logs_dropped_total" in text  # the rest of /metrics is served


def test_other_processes_export_no_proxy_series() -> None:
    text = _render("db_update")
    assert "proxy_" not in text
    assert "telegram_outage" not in text
