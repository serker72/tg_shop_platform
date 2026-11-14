"""OpenTelemetry tracing: global ``TracerProvider`` exporting OTLP gRPC to Alloy.

``init_telemetry`` is a no-op (NoOp/Proxy provider stays global) when
``OTEL_SDK_DISABLED=true`` or ``OTEL_EXPORTER_OTLP_ENDPOINT`` is empty, so the bots start and
work without a collector. Export is asynchronous (``BatchSpanProcessor``); every exported span
goes through :class:`ScrubbingSpanExporter`, which applies the Sentry scrubbing rules
(:func:`app.core.observability.sentry.scrub`) to span names, attributes, events and status.
Errors stay with ``sentry_sdk`` (no ``SentrySpanProcessor``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Status
from opentelemetry.util.types import Attributes

from app.core.observability.sentry import FILTERED, is_secret_key, resolve_release, scrub

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from app.core.config import Settings

__all__ = [
    "ScrubbingSpanExporter",
    "current_span_ids",
    "get_tracer",
    "get_tracer_provider",
    "init_telemetry",
    "instrument_engine",
    "scrub_attributes",
    "shutdown_telemetry",
]

_provider: TracerProvider | None = None


def get_tracer_provider() -> trace.TracerProvider:
    """Provider configured by ``init_telemetry`` or the global (NoOp/Proxy) one."""
    return _provider if _provider is not None else trace.get_tracer_provider()


def get_tracer(name: str) -> trace.Tracer:
    return get_tracer_provider().get_tracer(name)


def current_span_ids() -> tuple[str, str] | None:
    """``(trace_id 32 hex, span_id 16 hex)`` of the active span, ``None`` without one."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        return tuple(scrub(v) if isinstance(v, str) else v for v in value)
    return value


def scrub_attributes(attributes: Attributes) -> dict[str, Any]:
    """Mask secret-named keys and credentials/bot tokens in string values."""
    if not attributes:
        return {}
    return {
        key: FILTERED if is_secret_key(key) else _scrub_value(value)
        for key, value in attributes.items()
    }


def _scrub_span(span: ReadableSpan) -> ReadableSpan:
    status = span.status
    if status.description:
        status = Status(status.status_code, scrub(status.description))
    return ReadableSpan(
        name=scrub(span.name),
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=scrub_attributes(span.attributes),
        events=[Event(e.name, scrub_attributes(e.attributes), e.timestamp) for e in span.events],
        links=span.links,
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class ScrubbingSpanExporter(SpanExporter):
    """Exporter wrapper: scrubs every span before handing it to ``delegate``."""

    def __init__(self, delegate: SpanExporter) -> None:
        self._delegate = delegate

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._delegate.export([_scrub_span(s) for s in spans])

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)


def build_tracer_provider(settings: Settings, *, service_name: str) -> TracerProvider:
    """Provider with resource and sampler, without span processors."""
    obs = settings.observability
    resource = Resource.create(
        {
            "service.name": obs.otel_service_name or service_name,
            "service.version": resolve_release(settings),
            "deployment.environment": obs.sentry_environment or settings.project_environment,
        }
    )
    sampler = ParentBased(TraceIdRatioBased(obs.otel_traces_sampler_ratio))
    return TracerProvider(resource=resource, sampler=sampler)


def init_telemetry(settings: Settings, *, service_name: str, set_global: bool = True) -> bool:
    """Configure tracing; return whether it is enabled (False = NoOp, nothing exported)."""
    global _provider
    obs = settings.observability
    endpoint = obs.otel_exporter_otlp_endpoint
    if obs.otel_sdk_disabled or not endpoint:
        return False
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    provider = build_tracer_provider(settings, service_name=service_name)
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=urlsplit(endpoint).scheme != "https")
    provider.add_span_processor(BatchSpanProcessor(ScrubbingSpanExporter(exporter)))
    _provider = provider
    if set_global:
        trace.set_tracer_provider(provider)
    return True


def shutdown_telemetry(timeout_millis: int = 2000) -> None:
    """Flush pending spans and stop the exporter (no-op when tracing is disabled)."""
    global _provider
    if _provider is None:
        return
    _provider.force_flush(timeout_millis)
    _provider.shutdown()
    _provider = None


def instrument_engine(engine: AsyncEngine) -> None:
    """SQLAlchemy spans for ``engine`` (only when ``init_telemetry`` enabled tracing).

    ``opentelemetry-instrumentation-sqlalchemy`` 0.66b2 declares ``sqlalchemy < 2.1``;
    the dependency check is skipped (spans verified on SQLAlchemy 2.1.8).
    """
    if _provider is None:
        return
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    instrumentor = SQLAlchemyInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        return
    instrumentor.instrument(
        engine=engine.sync_engine, tracer_provider=_provider, skip_dep_check=True
    )
