"""Unit tests of ``app.core.observability`` (no infrastructure)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
import sentry_sdk
from aiogram.types import Update
from aiohttp.test_utils import TestClient, TestServer
from faststream.rabbit import TestRabbitBroker

from app.core.config import Settings
from app.core.logging import bind_trace_id, clear_trace_id, configure_logging, get_trace_id
from app.core.observability.broker import BROKER_LOGGER_NAME, build_rabbit_broker
from app.core.observability.health import (
    HealthCheck,
    HealthChecker,
    HealthReportCache,
    HeartbeatRegistry,
    has_live_heartbeat,
    heartbeat_key,
    publish_heartbeat,
)
from app.core.observability.http_server import create_app
from app.core.observability.metrics import build_metrics, render_metrics
from app.core.observability.providers import build_probe_storage_client
from app.core.observability.sentry import (
    FILTERED,
    before_send,
    init_sentry,
    is_secret_key,
    scrub,
)
from app.core.observability.tracing import TRACE_ID_HEADER, AiogramTraceIdMiddleware
from app.modules.logging.metrics import DROP_REASONS, LogsDroppedCounter


def test_logs_dropped_exported() -> None:
    counter = LogsDroppedCounter()
    counter.inc(3, reason="queue_full")
    counter.inc(1, reason="db_error")
    metrics = build_metrics(counter)
    text = render_metrics(metrics.registry).decode()
    assert 'logs_dropped_total{reason="queue_full"} 3.0' in text
    assert 'logs_dropped_total{reason="db_error"} 1.0' in text
    counter.inc(2, reason="queue_full")
    assert (
        'logs_dropped_total{reason="queue_full"} 5.0' in render_metrics(metrics.registry).decode()
    )
    assert counter.value == 6  # the counter API is unchanged


def test_logs_dropped_known_reasons_zero() -> None:
    text = render_metrics(build_metrics(LogsDroppedCounter()).registry).decode()
    assert "# TYPE logs_dropped_total counter" in text
    for reason in DROP_REASONS:
        assert f'logs_dropped_total{{reason="{reason}"}} 0.0' in text


async def test_di_shares_dropped_counter_between_buffer_and_metrics(tmp_path: Any) -> None:
    from uuid import uuid4

    from dishka import make_async_container
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.observability.metrics import AppMetrics
    from app.core.observability.providers import ObservabilityProvider
    from app.modules.logging.buffer import LogBuffer
    from app.modules.logging.events import ActivityEvent
    from app.modules.logging.files import DailyFileWriter
    from app.modules.logging.ioc import LoggingProvider
    from app.modules.logging.metrics import logs_dropped_total

    # Same wiring as ``run_service``; only the two APP factories below are resolved.
    container = make_async_container(
        LoggingProvider(), ObservabilityProvider(), skip_validation=True
    )
    counter = await container.get(LogsDroppedCounter)
    try:
        metrics = await container.get(AppMetrics)
        assert counter is logs_dropped_total
        assert metrics.logs_dropped is counter
        counter.reset()
        # LogBuffer fed by the DI counter, never started: log() drops with not_running.
        buffer = LogBuffer(async_sessionmaker(), DailyFileWriter(tmp_path), counter=counter)
        buffer.log(ActivityEvent(bot_id=uuid4(), action_type="CLICK"))
        text = render_metrics(metrics.registry).decode()
        assert 'logs_dropped_total{reason="not_running"} 1.0' in text
    finally:
        counter.reset()
        await container.close()


def test_skeleton_metrics_registered() -> None:
    metrics = build_metrics(LogsDroppedCounter())
    metrics.app_errors_total.labels(component="x").inc()
    metrics.update_duration_seconds.observe(0.1)
    metrics.active_bots.set(2)
    text = render_metrics(metrics.registry).decode()
    assert 'app_errors_total{component="x"} 1.0' in text
    assert "update_duration_seconds_count 1.0" in text
    assert "active_bots 2.0" in text


async def _ok() -> None:
    return None


async def _boom() -> None:
    raise RuntimeError("amqp://user:secret@host")


async def _slow() -> None:
    await asyncio.sleep(5)


async def test_health_ok_fail_timeout() -> None:
    checker = HealthChecker(
        [HealthCheck("a", _ok), HealthCheck("b", _boom), HealthCheck("c", _slow)], timeout=0.05
    )
    report = await checker.run()
    statuses = {c.name: c.status for c in report.checks}
    assert statuses == {"a": "ok", "b": "fail", "c": "timeout"}
    assert report.ready is False
    assert report.as_dict()["checks"]["b"]["error"] == "RuntimeError"  # no message leaked


async def test_non_critical_failure_is_degraded() -> None:
    checker = HealthChecker([HealthCheck("a", _ok), HealthCheck("w", _boom, False)], timeout=1)
    report = await checker.run()
    assert report.ready is True
    assert report.status == "degraded"
    assert report.checks[1].status == "degraded"


class _FakePipe:
    def __init__(self, redis: _FakeZsetRedis) -> None:
        self.redis = redis
        self.ops: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> _FakePipe:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.ops.append(("zadd", (key, mapping)))

    def zremrangebyscore(self, key: str, lo: str, hi: float) -> None:
        self.ops.append(("zrem", (key, hi)))

    def expire(self, key: str, ttl: int) -> None:
        self.ops.append(("expire", (key, ttl)))

    async def execute(self) -> None:
        for name, args in self.ops:
            zset = self.redis.data.setdefault(args[0], {})
            if name == "zadd":
                zset.update(args[1])
            elif name == "zrem":
                for member in [m for m, s in zset.items() if s <= args[1]]:
                    del zset[member]
            else:
                self.redis.expires[args[0]] = args[1]


class _FakeZsetRedis:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, float]] = {}
        self.expires: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> _FakePipe:
        return _FakePipe(self)

    async def zcount(self, key: str, lo: float, hi: str) -> int:
        return sum(1 for s in self.data.get(key, {}).values() if s >= lo)

    async def scan_iter(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("heartbeat must not SCAN")


async def test_heartbeat_zset_publish_and_check() -> None:
    redis: Any = _FakeZsetRedis()
    assert heartbeat_key("bot_worker") == "heartbeat:bot_worker"
    await publish_heartbeat(redis, "bot_worker", "old", ttl=30, now=1000.0)
    await publish_heartbeat(redis, "bot_worker", "new", ttl=30, now=1040.0)
    # "old" (score 1000 <= 1040 - 30) was pruned by ZREMRANGEBYSCORE.
    assert redis.data["heartbeat:bot_worker"] == {"new": 1040.0}
    assert redis.expires["heartbeat:bot_worker"] == 30
    assert await has_live_heartbeat(redis, "bot_worker", ttl=30, now=1050.0)
    assert not await has_live_heartbeat(redis, "bot_worker", ttl=30, now=1071.0)
    assert not await has_live_heartbeat(redis, "missing", ttl=30)


async def test_heartbeat_registry() -> None:
    registry = HeartbeatRegistry()
    registry.register("archiver")
    registry.register("bot_worker", required=True)
    redis: Any = _FakeZsetRedis()
    await publish_heartbeat(redis, "bot_worker", "host-1", ttl=30)
    report = await HealthChecker(registry.checks(redis, ttl=30), timeout=1).run()
    by_name = {c.name: c.status for c in report.checks}
    assert by_name == {"heartbeat:archiver": "degraded", "heartbeat:bot_worker": "ok"}
    assert report.ready is True
    assert HeartbeatRegistry().checks(redis, ttl=30) == []


async def test_ready_cache_single_flight_and_ttl() -> None:
    calls = 0
    gate = asyncio.Event()

    async def probe() -> None:
        nonlocal calls
        calls += 1
        await gate.wait()

    now = [100.0]
    cache = HealthReportCache(
        HealthChecker([HealthCheck("x", probe)], timeout=1), ttl=1.0, clock=lambda: now[0]
    )
    tasks = [asyncio.create_task(cache.get()) for _ in range(5)]
    await asyncio.sleep(0)
    gate.set()
    reports = await asyncio.gather(*tasks)
    assert calls == 1
    assert all(r is reports[0] for r in reports)
    now[0] = 100.5
    assert await cache.get() is reports[0]
    assert calls == 1
    now[0] = 101.1
    await cache.get()
    assert calls == 2


def test_probe_storage_client_has_short_timeouts() -> None:
    settings = Settings(_env_file=None, observability={"health_timeout_seconds": 1.5})
    client = build_probe_storage_client(settings)
    try:
        config = client.meta.config
        assert config.connect_timeout == 1.5
        assert config.read_timeout == 1.5
        # botocore normalises ``max_attempts`` (retries) to ``total_max_attempts`` (calls).
        assert (
            config.retries.get("total_max_attempts", config.retries.get("max_attempts", 0) + 1) == 1
        )
    finally:
        client.close()


async def test_http_endpoints() -> None:
    metrics = build_metrics(LogsDroppedCounter())
    for probe, code in ((_ok, 200), (_boom, 503)):
        app = create_app(
            HealthChecker([HealthCheck("db", probe)], timeout=1),
            metrics.registry,
            ready_cache_ttl=0,
        )
        async with TestClient(TestServer(app)) as client:
            live = await client.get("/health/live")
            assert live.status == 200
            ready = await client.get("/health/ready")
            assert ready.status == code
            body = await ready.json()
            assert body["ready"] is (code == 200)
            assert "db" in body["checks"]
            resp = await client.get("/metrics")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/plain")
            assert "active_bots" in await resp.text()


def test_sentry_disabled_without_dsn() -> None:
    settings = Settings(_env_file=None, observability={"sentry_dsn": ""})
    assert init_sentry(settings, component="test") is False


def test_scrub_secrets() -> None:
    event: Any = {
        "extra": {"password": "p", "bot_token": "t", "Authorization": "Bearer x", "n": 1},
        "message": "connect amqp://user:pw@rabbit:5672/ token 123456789:" + "A" * 35,
        "breadcrumbs": {"values": [{"data": {"SENTRY_DSN": "https://k@glitchtip/1"}}]},
    }
    cleaned: Any = before_send(event, {})
    assert cleaned["extra"] == {
        "password": FILTERED,
        "bot_token": FILTERED,
        "Authorization": FILTERED,
        "n": 1,
    }
    assert "pw" not in cleaned["message"]
    assert "AAAA" not in cleaned["message"]
    assert cleaned["breadcrumbs"]["values"][0]["data"]["SENTRY_DSN"] == FILTERED
    assert scrub("plain text") == "plain text"


BOT_TOKEN = "123456789:" + "AAbb_-" * 6


def test_scrub_bot_token_in_url_and_breadcrumbs() -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    assert "123456789" not in scrub(url)
    assert scrub(url) == f"https://api.telegram.org/bot{FILTERED}/getMe"
    event: Any = {
        "request": {"url": url},
        "breadcrumbs": {"values": [{"message": f"POST {url}", "data": {"url": url}}]},
    }
    assert "123456789" not in str(before_send(event, {}))


def test_before_send_transaction_scrubs_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda *a: None)
    settings = Settings(_env_file=None, observability={"sentry_dsn": "https://k@gt.local/1"})
    assert init_sentry(settings, component="test") is True
    hook = captured["before_send_transaction"]
    transaction: Any = {
        "transaction": "update",
        "spans": [{"description": f"GET https://api.telegram.org/bot{BOT_TOKEN}/getMe"}],
    }
    assert "123456789" not in str(hook(transaction, {}))
    assert captured["before_send"] is before_send


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "db_password",
        "bot_token",
        "Authorization",
        "X-Api-Key",
        "api_key",
        "apiKey",
        "SENTRY_DSN",
        "client.secret",
        "access_key",
        "accessToken",
    ],
)
def test_secret_keys_masked(key: str) -> None:
    assert is_secret_key(key)
    assert scrub({key: "v"}) == {key: FILTERED}


@pytest.mark.parametrize(
    "key", ["author", "compass", "tokens_count", "passport", "dsname", "keyboard", "n"]
)
def test_innocent_keys_kept(key: str) -> None:
    assert not is_secret_key(key)
    assert scrub({key: "v"}) == {key: "v"}


@pytest.fixture
def _restore_faststream_logger() -> Any:
    """Importing FastStream configures the module-level ``faststream`` logger (own stderr
    handler, ``propagate=False``); test_logging uses that name, so reset it afterwards."""
    yield
    log = logging.getLogger("faststream")
    log.handlers.clear()
    log.propagate = True
    log.setLevel(logging.NOTSET)


@pytest.mark.usefixtures("_restore_faststream_logger")
async def test_faststream_trace_id_roundtrip() -> None:
    broker = build_rabbit_broker(Settings(_env_file=None))
    seen: list[str | None] = []

    @broker.subscriber("trace-q")
    async def handler(msg: str) -> None:
        seen.append(get_trace_id())

    async with TestRabbitBroker(broker) as br:
        await br.publish("m", queue="trace-q", headers={TRACE_ID_HEADER: "explicit"})
        token = bind_trace_id("bound")
        try:
            await br.publish("m", queue="trace-q")
        finally:
            clear_trace_id(token)
        await br.publish("m", queue="trace-q")
    assert seen[:2] == ["explicit", "bound"]
    assert seen[2] and len(seen[2]) == 32
    assert get_trace_id() is None


@pytest.mark.usefixtures("_restore_faststream_logger")
async def test_broker_logs_go_to_json_pipeline(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_logs=True)
    broker = build_rabbit_broker(Settings(_env_file=None))

    # TestRabbitBroker swaps the logger out, so log through the broker's own logger state
    # (the object every subscriber/publisher ``_log`` call goes through).
    broker._setup_logger()
    token = bind_trace_id("t-log")
    try:
        broker.config.logger.log("broker probe", logging.INFO)
    finally:
        clear_trace_id(token)
    out = capsys.readouterr().out
    records = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    broker_records = [r for r in records if r.get("logger") == BROKER_LOGGER_NAME]
    assert broker_records, out
    assert any(r.get("trace_id") == "t-log" for r in broker_records)
    # FastStream's default access logger stays unused (no plain-text duplicates).
    assert not logging.getLogger("faststream.access.rabbit").handlers


async def test_aiogram_trace_middleware() -> None:
    seen: list[str | None] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        seen.append(get_trace_id())
        assert data["trace_id"] == get_trace_id()
        return "done"

    middleware = AiogramTraceIdMiddleware()
    update = Update(update_id=1)
    assert await middleware(handler, update, {}) == "done"
    await middleware(handler, update, {})
    assert seen[0] and seen[1] and seen[0] != seen[1]
    assert get_trace_id() is None


@pytest.fixture(autouse=True)
def _no_trace_leak() -> Any:
    clear_trace_id()
    yield
    clear_trace_id()
