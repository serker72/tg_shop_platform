"""Unit tests of OpenTelemetry tracing (in-memory exporter, no network)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any

import pytest
import sentry_sdk
import structlog
from aiogram.types import Update
from faststream.rabbit import TestRabbitBroker
from faststream.rabbit.annotations import RabbitMessage
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanContext, StatusCode, get_current_span
from sentry_sdk.integrations import logging as sentry_logging
from sqlalchemy.pool import NullPool

from app.core import telemetry
from app.core.config import Settings
from app.core.logging import (
    bind_trace_id,
    clear_trace_id,
    configure_logging,
    get_logger,
    get_trace_id,
)
from app.core.observability import sentry as sentry_mod
from app.core.observability.broker import build_rabbit_broker
from app.core.observability.sentry import FILTERED, before_send
from app.core.observability.tracing import (
    TRACE_ID_HEADER,
    UPDATE_SPAN_NAME,
    AiogramTracingMiddleware,
)
from app.shared_kernel.infrastructure.database import build_engine

pytestmark = pytest.mark.req("R8")

_BOT_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
_TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(telemetry.ScrubbingSpanExporter(memory)))
    monkeypatch.setattr(telemetry, "_provider", provider)
    yield memory
    provider.shutdown()


@pytest.fixture
def _faststream_logger() -> Iterator[None]:
    yield
    log = logging.getLogger("faststream")
    log.handlers.clear()
    log.propagate = True
    log.setLevel(logging.NOTSET)


def _hex_ids() -> tuple[str, str]:
    ids = telemetry.current_span_ids()
    assert ids is not None
    return ids


def test_span_attributes_scrubbed(exporter: InMemorySpanExporter) -> None:
    url = f"https://api.telegram.org/bot{_BOT_TOKEN}/getMe"
    tracer = telemetry.get_tracer("t")
    with tracer.start_as_current_span(
        f"GET /bot{_BOT_TOKEN}/getMe",
        attributes={
            "http.url": url,
            "bot_token": "plain",
            "db.url": "postgresql://app:s3cret@db:5432/app",
            "ok": 1,
        },
    ) as span:
        span.add_event("retry", {"url": url})
    (exported,) = exporter.get_finished_spans()
    attrs = exported.attributes or {}
    assert _BOT_TOKEN not in exported.name
    assert attrs["http.url"] == f"https://api.telegram.org/bot{FILTERED}/getMe"
    assert attrs["bot_token"] == FILTERED
    assert "s3cret" not in str(attrs["db.url"])
    assert attrs["ok"] == 1
    assert _BOT_TOKEN not in str((exported.events[0].attributes or {})["url"])


def test_log_has_otel_ids(
    exporter: InMemorySpanExporter, capsys: pytest.CaptureFixture[str]
) -> None:
    structlog.reset_defaults()
    configure_logging(json_logs=True)
    with telemetry.get_tracer("t").start_as_current_span("s"):
        trace_id, span_id = _hex_ids()
        get_logger("t").info("inside")
    get_logger("t").info("outside")
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    inside = next(r for r in lines if r["event"] == "inside")
    outside = next(r for r in lines if r["event"] == "outside")
    assert inside["trace_id"] == trace_id and re.fullmatch(r"[0-9a-f]{32}", trace_id)
    assert inside["span_id"] == span_id and re.fullmatch(r"[0-9a-f]{16}", span_id)
    assert "span_id" not in outside
    structlog.reset_defaults()


@pytest.mark.usefixtures("_faststream_logger")
async def test_rabbit_traceparent_roundtrip(exporter: InMemorySpanExporter) -> None:
    broker = build_rabbit_broker(Settings(_env_file=None))
    seen: list[tuple[str, str]] = []

    @broker.subscriber("otel-q")
    async def handler(msg: str, message: RabbitMessage) -> None:
        ctx = get_current_span().get_span_context()
        seen.append((format(ctx.trace_id, "032x"), str(message.headers.get("traceparent"))))

    async with TestRabbitBroker(broker) as br:
        with telemetry.get_tracer("t").start_as_current_span("producer"):
            trace_id, _ = _hex_ids()
            await br.publish("m", queue="otel-q")
    assert seen
    consumed_trace_id, traceparent = seen[0]
    assert consumed_trace_id == trace_id
    assert _TRACEPARENT_RE.match(traceparent)
    assert traceparent.split("-")[1] == trace_id
    assert {s.context.trace_id for s in exporter.get_finished_spans()} == {int(trace_id, 16)}


async def test_aiogram_update_span(exporter: InMemorySpanExporter) -> None:
    seen: list[str] = []

    async def handler(event: Any, data: dict[str, Any]) -> None:
        seen.append(data["trace_id"])
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await AiogramTracingMiddleware()(handler, Update(update_id=7), {})
    (span,) = exporter.get_finished_spans()
    assert span.name == UPDATE_SPAN_NAME
    assert span.parent is None
    assert format(span.context.trace_id, "032x") == seen[0]
    assert (span.attributes or {})["telegram.update_id"] == 7
    assert span.status.status_code is StatusCode.ERROR


def test_sentry_event_gets_otel_trace_id(exporter: InMemorySpanExporter) -> None:
    with telemetry.get_tracer("t").start_as_current_span("s"):
        trace_id, span_id = _hex_ids()
        event = before_send({"message": f"bot{_BOT_TOKEN}"}, {})
    assert event is not None
    assert event["contexts"]["trace"]["trace_id"] == trace_id
    assert event["contexts"]["trace"]["span_id"] == span_id
    assert _BOT_TOKEN not in str(event)


@pytest.mark.parametrize(
    "env",
    [
        {"OTEL_EXPORTER_OTLP_ENDPOINT": ""},
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://alloy:4317", "OTEL_SDK_DISABLED": "true"},
    ],
)
def test_disabled_is_noop(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(telemetry, "_provider", None)
    settings = Settings(_env_file=None)
    assert telemetry.init_telemetry(settings, service_name="master_bot") is False
    with telemetry.get_tracer("t").start_as_current_span("s"):
        assert telemetry.current_span_ids() is None
    telemetry.shutdown_telemetry()


def test_enabled_without_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setattr(telemetry, "_provider", None)
    settings = Settings(_env_file=None)
    assert telemetry.init_telemetry(settings, service_name="bot_worker", set_global=False)
    provider = telemetry.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert provider.resource.attributes["service.name"] == "bot_worker"
    telemetry.shutdown_telemetry(timeout_millis=100)


def test_public_otlp_endpoint_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.example.com:4317")
    with pytest.raises(ValueError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        Settings(_env_file=None)


def _otel_settings() -> Settings:
    return Settings(
        _env_file=None, observability={"otel_exporter_otlp_endpoint": "http://127.0.0.1:1"}
    )


def test_event_names_and_links_scrubbed(exporter: InMemorySpanExporter) -> None:
    url = f"https://api.telegram.org/bot{_BOT_TOKEN}/getMe"
    linked = SpanContext(trace_id=1, span_id=1, is_remote=False)
    tracer = telemetry.get_tracer("t")
    with tracer.start_as_current_span("s", links=[Link(linked, {"url": url, "token": "x"})]) as sp:
        sp.add_event(f"retry {url}")
    (exported,) = exporter.get_finished_spans()
    assert _BOT_TOKEN not in exported.events[0].name
    link_attrs = exported.links[0].attributes or {}
    assert _BOT_TOKEN not in str(link_attrs["url"])
    assert link_attrs["token"] == FILTERED


def test_dropped_counters_kept() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider(span_limits=SpanLimits(max_span_attributes=1, max_events=1))
    provider.add_span_processor(SimpleSpanProcessor(telemetry.ScrubbingSpanExporter(memory)))
    with provider.get_tracer("t").start_as_current_span("s", attributes={"a": 1, "b": 2}) as sp:
        sp.add_event("e1")
        sp.add_event("e2")
    (exported,) = memory.get_finished_spans()
    assert exported.dropped_attributes == 1
    assert exported.dropped_events == 1
    provider.shutdown()


def test_init_is_idempotent_and_instruments_every_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry, "_provider", None)
    settings = _otel_settings()
    assert telemetry.init_telemetry(
        settings, service_name="t", set_global=False, exporter=InMemorySpanExporter()
    )
    first = telemetry.get_tracer_provider()
    try:
        assert telemetry.init_telemetry(settings, service_name="t", set_global=False)
        assert telemetry.get_tracer_provider() is first
        engines = [build_engine(settings, poolclass=NullPool) for _ in range(2)]
        for engine in engines:
            assert len(engine.sync_engine.dispatch.before_cursor_execute) > 0
    finally:
        telemetry.shutdown_telemetry(timeout_millis=100)
    plain = build_engine(settings, poolclass=NullPool)
    assert len(plain.sync_engine.dispatch.before_cursor_execute) == 0


@pytest.mark.usefixtures("_faststream_logger")
async def test_rabbit_trace_id_fallback_without_otel() -> None:
    broker = build_rabbit_broker(Settings(_env_file=None))
    seen: list[str | None] = []

    @broker.subscriber("fallback-q")
    async def handler(msg: str) -> None:
        seen.append(get_trace_id())

    async with TestRabbitBroker(broker) as br:
        token = bind_trace_id("f" * 32)
        try:
            await br.publish("m", queue="fallback-q")
        finally:
            clear_trace_id(token)
        await br.publish("m", queue="fallback-q", headers={TRACE_ID_HEADER: "explicit"})
        await br.publish("m", queue="fallback-q")
    assert seen[:2] == ["f" * 32, "explicit"]
    assert seen[2] and len(seen[2]) == 32
    assert get_trace_id() is None


def test_sentry_trace_context_only_otel_ids(exporter: InMemorySpanExporter) -> None:
    event: Any = {"contexts": {"trace": {"trace_id": "0" * 32, "parent_span_id": "1", "op": "x"}}}
    with telemetry.get_tracer("t").start_as_current_span("s"):
        trace_id, span_id = _hex_ids()
        cleaned = before_send(event, {})
    assert cleaned is not None
    assert cleaned["contexts"]["trace"] == {"trace_id": trace_id, "span_id": span_id}


def test_sentry_init_no_propagation_and_otel_loggers_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda *a: None)
    settings = Settings(_env_file=None, observability={"sentry_dsn": "http://k@caddy:8081/1"})
    assert sentry_mod.init_sentry(settings, component="t") is True
    assert captured["trace_propagation_targets"] == []
    assert captured["traces_sample_rate"] is None
    assert set(sentry_mod.OTEL_EXPORT_LOGGERS) <= sentry_logging._IGNORED_LOGGERS


def test_sampler_arg_standard_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.25")
    assert Settings(_env_file=None).observability.otel_traces_sampler_ratio == 0.25
