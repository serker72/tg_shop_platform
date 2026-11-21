"""Unit tests of ``app.core.observability`` (no infrastructure)."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import subprocess
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
import sentry_sdk
import yaml
from aiogram.types import Update
from aiohttp.test_utils import TestClient, TestServer

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
from app.core.observability.tracing import AiogramTracingMiddleware
from app.modules.logging.metrics import DROP_REASONS, LogsDroppedCounter
from ops.scripts import check_maintenance_observability as maintenance_smoke

pytestmark = pytest.mark.req("R8")


@pytest.mark.req("S5")
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


@pytest.mark.req("S5")
def test_logs_dropped_known_reasons_zero() -> None:
    text = render_metrics(build_metrics(LogsDroppedCounter()).registry).decode()
    assert "# TYPE logs_dropped_total counter" in text
    for reason in DROP_REASONS:
        assert f'logs_dropped_total{{reason="{reason}"}} 0.0' in text


@pytest.mark.req("S5")
async def test_di_shares_dropped_counter_between_buffer_and_metrics(tmp_path: Any) -> None:

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
        buffer.log(ActivityEvent(telegram_bot_id=1, action_type="CLICK"))
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


def test_maintenance_stat_only_shows_a_live_flag() -> None:
    obs = Path(__file__).resolve().parents[2] / "ops" / "observability"
    queries = yaml.safe_load((obs / "alloy/postgres_queries.yml").read_text(encoding="utf-8"))
    assert queries["system"]["metrics"][0]["maintenance_mode"]["usage"] == "GAUGE"

    path = obs / "grafana/dashboards/platform_overview.json"
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    (panel,) = [panel for panel in dashboard["panels"] if panel["id"] == 14]
    assert panel["title"] == "Maintenance mode at range end"
    assert "end of the selected time range" in panel["description"]
    assert "ending at now for live status" in panel["description"]
    assert panel["type"] == "stat"
    (target,) = panel["targets"]
    assert target["instant"] is True and target["range"] is False
    assert target["expr"].count(" and on (env, host, job, instance) ") == 2
    for metric in (
        'system_maintenance_mode{env=~"$env", host=~"$host", job="postgres"}',
        'pg_up{job="postgres"}',
        'up{job="postgres"}',
    ):
        assert f"time() - timestamp({metric}) <= 45" in target["expr"]
    defaults = panel["fieldConfig"]["defaults"]
    assert defaults["noValue"] == "No data"
    (mapping,) = defaults["mappings"]
    assert mapping["type"] == "value"
    assert {key: value["text"] for key, value in mapping["options"].items()} == {
        "0": "OFF",
        "1": "ON",
    }


def test_logs_dashboard_has_system_settings_events_panel() -> None:
    path = Path(__file__).resolve().parents[2] / "ops/observability/grafana/dashboards/logs.json"
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    (panel,) = [panel for panel in dashboard["panels"] if panel["id"] == 5]
    assert panel["title"] == "System settings changed events"
    assert panel["type"] == "logs"
    target = panel["targets"][0]
    query = target["expr"]
    assert query == (
        '{env=~"$env", host=~"$host", service=~"$service"} '
        '| json | event="system_settings_changed" '
        '| line_format "{{.event}} action={{.action}} actor_id={{.actor_id}} '
        '{{__line__}}"'
    )
    assert "toJson" not in query
    assert "{{.before}}" not in query and "{{.after}}" not in query
    assert "{{__line__}}" in query
    assert "before={{" not in query and "after={{" not in query
    assert target["queryType"] == "range"

    docs = (Path(__file__).resolve().parents[2] / "docs/ops/observability.md").read_text(
        encoding="utf-8"
    )
    assert '  |= "\\"retention_days\\":30"' in docs
    assert '  |= "\\\\"retention_days\\\\":30"' not in docs
    for snippet in (
        '| json before_retention_days="before.retention_days"',
        '| before_retention_days="90"',
        '| json after_retention_days="after.retention_days"',
        '| after_retention_days="30"',
        (
            '| json before_retention_days="before.retention_days", '
            'after_retention_days="after.retention_days"'
        ),
    ):
        assert snippet in docs
    assert "`event`," in docs and "`action` и `actor_id` после `| json`" in docs
    assert "`before_<key>`/`after_<key>`" in docs
    assert "`{{__line__}}`" in docs
    assert "object template" in docs
    assert "toJson" in docs
    assert "`before` и `after` остаются\nобъектами:" not in docs


def test_maintenance_smoke_disables_proxy_only_for_its_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.request import ProxyHandler, build_opener

    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    assert not any(
        isinstance(handler, ProxyHandler)
        for handler in getattr(maintenance_smoke.local_http_client(), "handlers", ())
    )
    assert any(
        isinstance(handler, ProxyHandler) for handler in getattr(build_opener(), "handlers", ())
    )


@pytest.mark.parametrize("interrupted", ["network", "container"])
def test_maintenance_smoke_registers_cleanup_when_sigterm_arrives_during_creation(
    monkeypatch: pytest.MonkeyPatch, interrupted: str
) -> None:
    cancel = maintenance_smoke.Cancellation()
    removed: list[str] = []

    def docker(*args: str, timeout: float = 90) -> str:
        if args[1] == "ls":
            return f"{args[0]}-id"
        if args[1] == "rm":
            removed.append(args[-1])
            cancel.request(signal.SIGTERM, None)
        elif args[-1] == interrupted:
            cancel.request(signal.SIGTERM, None)
        return ""

    monkeypatch.setattr(maintenance_smoke, "docker", docker)
    with pytest.raises(SystemExit) as exit_info, ExitStack() as cleanup:
        cancel.acquire(cleanup, "network", "run-id", "network")
        cancel.acquire(cleanup, "container", "run-id", "container", "container")
    assert exit_info.value.code == 143
    expected = ["network-id"] if interrupted == "network" else ["container-id", "network-id"]
    assert removed == expected


@pytest.mark.parametrize(
    ("kind", "failure", "appearance"),
    [
        (kind, failure, appearance)
        for kind in ("container", "network")
        for failure, appearance in (
            ("timeout", "immediate"),
            ("timeout", "late"),
            ("timeout", "never"),
            ("interrupt", "late"),
            ("exit", "immediate"),
            ("exit", "late"),
        )
    ]
    + [pytest.param("network", "exit-1", "late", id="network-exit-1-late")],
)
def test_maintenance_smoke_cleans_partial_docker_creation_by_owned_id(
    monkeypatch: pytest.MonkeyPatch,
    kind: maintenance_smoke.ResourceKind,
    failure: str,
    appearance: str,
) -> None:
    cancel = maintenance_smoke.Cancellation()
    key = maintenance_smoke.RESOURCE_LABEL
    # Same resource name, different run. Docker must never remove it by name.
    resources = {"foreign-id": f"{key}=other-run:pg"}
    removed: list[str] = []
    error = {
        "timeout": subprocess.TimeoutExpired("create", timeout=90),
        "interrupt": KeyboardInterrupt(),
        "exit": subprocess.CalledProcessError(125, "create"),
        "exit-1": subprocess.CalledProcessError(1, "network create"),
    }[failure]
    pending: dict[str, str] = {}
    elapsed = 0.0
    lookups = 0

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        assert seconds > 0
        elapsed += seconds
        assert elapsed <= maintenance_smoke.LATE_CREATE_GRACE

    def docker(*args: str, timeout: float = 90) -> str:
        nonlocal lookups
        if args[1] == "ls":
            lookups += 1
            assert args[:2] == (kind, "ls")
            assert ("--all" in args) is (kind == "container")
            assert "--no-trunc" in args
            assert args[-1] == f"label={key}=this-run:pg"
            ids = [rid for rid, label in resources.items() if f"label={label}" == args[-1]]
            resources.update(pending)
            pending.clear()
            assert 0 < timeout <= maintenance_smoke.CLEANUP_TIMEOUT
            return "\n".join(ids)
        if args[1] == "rm":
            expected = (kind, "rm", "-fv") if kind == "container" else (kind, "rm")
            assert args == (*expected, "owned-id")
            removed.append(args[-1])
            del resources[args[-1]]
            return ""
        label = args[args.index("--label") + 1]
        if appearance != "never":
            (pending if appearance == "late" else resources)["owned-id"] = label
        if failure == "timeout":
            cancel.request(signal.SIGTERM, None)
        raise error

    monkeypatch.setattr(maintenance_smoke, "docker", docker)
    monkeypatch.setattr(maintenance_smoke.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(maintenance_smoke.time, "sleep", sleep)
    with pytest.raises(type(error)) as caught, ExitStack() as cleanup:
        cancel.acquire(cleanup, kind, "this-run", "pg", "postgres:test")
    assert caught.value is error
    assert cancel.requested is (failure == "timeout")
    if appearance == "never":
        assert elapsed == pytest.approx(maintenance_smoke.LATE_CREATE_GRACE)
        assert lookups > 1
        assert not removed
    else:
        assert lookups == (2 if appearance == "late" else 1)
        assert removed == ["owned-id"]
    assert resources == {"foreign-id": f"{key}=other-run:pg"}


@pytest.mark.parametrize("exit_code", [0, 1, 125])
def test_maintenance_smoke_promql_only_owns_foreground_promtool(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exit_code: int
) -> None:
    monkeypatch.setattr("sys.argv", ["smoke", "--promql-only"])
    label = ""
    removed: list[str] = []
    calls: list[str] = []
    failure = subprocess.CalledProcessError(exit_code, "promtool", output="test output")

    def docker(*args: str, timeout: float = 90) -> str:
        nonlocal label
        calls.append(args[0] if args[0] == "run" else args[1])
        if args[0] == "run":
            assert "-d" not in args and "--rm" in args
            assert "--entrypoint=promtool" in args
            name = args[args.index("--name") + 1]
            label = args[args.index("--label") + 1]
            assert label.startswith(f"{maintenance_smoke.RESOURCE_LABEL}=")
            assert label.endswith(f":{name}") and name.endswith("-promtool")
            if exit_code:
                raise failure
            return "SUCCESS"
        if args[:2] == ("container", "ls"):
            assert args[-1] == f"label={label}"
            assert "--all" in args and "--no-trunc" in args
            return "promtool-full-id" if exit_code == 125 else ""
        assert args == ("container", "rm", "-fv", "promtool-full-id")
        removed.append(args[-1])
        return ""

    monkeypatch.setattr(maintenance_smoke, "docker", docker)
    if exit_code:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            maintenance_smoke.main()
        assert caught.value is failure
    else:
        maintenance_smoke.main()
        assert capsys.readouterr().out == "SUCCESS\n"
    assert removed == (["promtool-full-id"] if exit_code == 125 else [])
    assert calls == (["run", "ls", "rm"] if exit_code == 125 else ["run", "ls"])


@pytest.mark.parametrize("original_failure", [False, True])
def test_maintenance_smoke_reports_cleanup_error_without_masking_original(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], original_failure: bool
) -> None:
    cleanup_error = subprocess.CalledProcessError(1, "list owned resources")
    create_error = subprocess.CalledProcessError(125, "create")

    def docker(*args: str, timeout: float = 90) -> str:
        if args[1] == "ls":
            raise cleanup_error
        if original_failure:
            raise create_error
        return ""

    monkeypatch.setattr(maintenance_smoke, "docker", docker)
    with pytest.raises(subprocess.CalledProcessError) as caught, ExitStack() as cleanup:
        maintenance_smoke.Cancellation().acquire(cleanup, "network", "run-id", "network")
    assert caught.value is (create_error if original_failure else cleanup_error)
    assert "Docker cleanup failed for network" in capsys.readouterr().err


@pytest.mark.parametrize("original_failure", [False, True])
def test_maintenance_smoke_defers_sigint_during_entire_cleanup(
    monkeypatch: pytest.MonkeyPatch, original_failure: bool
) -> None:
    previous = signal.getsignal(signal.SIGINT)
    cancel = maintenance_smoke.Cancellation()
    primary = RuntimeError("smoke failed")
    actions: list[tuple[str, ...]] = []

    def docker(*args: str, timeout: float = 90) -> str:
        if args[1] == "ls":
            actions.append(args[:2])
            signal.raise_signal(signal.SIGINT)
            return f"{args[0]}-id"
        if args[1] == "rm":
            actions.append((args[0], args[1], args[-1]))
        return ""

    monkeypatch.setattr(maintenance_smoke, "docker", docker)

    if original_failure:
        with pytest.raises(RuntimeError) as caught, maintenance_smoke.signal_handlers(cancel):
            with maintenance_smoke.DockerCleanup() as cleanup:
                assert getattr(signal.getsignal(signal.SIGINT), "__self__", None) is cancel
                cancel.acquire(cleanup, "network", "run-id", "network")
                cancel.acquire(cleanup, "container", "run-id", "container", "postgres:test")
                raise primary
        assert caught.value is primary
    else:
        with maintenance_smoke.signal_handlers(cancel):
            with maintenance_smoke.DockerCleanup() as cleanup:
                assert getattr(signal.getsignal(signal.SIGINT), "__self__", None) is cancel
                cancel.acquire(cleanup, "network", "run-id", "network")
                cancel.acquire(cleanup, "container", "run-id", "container", "postgres:test")
            with pytest.raises(SystemExit) as caught_exit:
                cancel.check()
        assert caught_exit.value.code == 130
    assert actions == [
        ("container", "ls"),
        ("container", "rm", "container-id"),
        ("network", "ls"),
        ("network", "rm", "network-id"),
    ]
    assert signal.getsignal(signal.SIGINT) is previous


def test_maintenance_smoke_preserves_cleanup_error_on_sigint() -> None:
    previous = signal.getsignal(signal.SIGINT)
    cleanup_error = subprocess.CalledProcessError(1, "docker container ls")
    cancel = maintenance_smoke.Cancellation()

    def interrupt_and_fail() -> None:
        signal.raise_signal(signal.SIGINT)
        raise cleanup_error

    with pytest.raises(subprocess.CalledProcessError) as caught:
        with maintenance_smoke.signal_handlers(cancel):
            with maintenance_smoke.DockerCleanup() as cleanup:
                cleanup.callback(interrupt_and_fail)
    assert caught.value is cleanup_error
    assert cancel.requested
    assert signal.getsignal(signal.SIGINT) is previous


def test_maintenance_smoke_first_signal_is_kept_through_cleanup() -> None:
    cancel = maintenance_smoke.Cancellation()
    callbacks: list[str] = []

    with maintenance_smoke.signal_handlers(cancel):
        with maintenance_smoke.DockerCleanup() as cleanup:
            cleanup.callback(lambda: callbacks.append("late"))
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGINT)
            callbacks.append("body")
        assert callbacks == ["body", "late"]
        with pytest.raises(SystemExit) as caught:
            cancel.check()
    assert caught.value.code == 143


def test_maintenance_smoke_cleanup_does_not_write_over_broken_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = subprocess.CalledProcessError(1, "docker container ls")

    def fail_stderr(*args: object, **kwargs: object) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(
        maintenance_smoke, "docker", lambda *args, **kwargs: (_ for _ in ()).throw(error)
    )
    monkeypatch.setattr("builtins.print", fail_stderr)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        with maintenance_smoke.DockerCleanup() as cleanup:
            maintenance_smoke.Cancellation().acquire(cleanup, "network", "run-id", "network")
    assert caught.value is error


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
        "breadcrumbs": {"values": [{"data": {"SENTRY_DSN": "https://k@urgentry/1"}}]},
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
    settings = Settings(_env_file=None, observability={"sentry_dsn": "http://k@caddy:8081/1"})
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

    middleware = AiogramTracingMiddleware()
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
