"""OpenTelemetry tracing: global ``TracerProvider`` exporting OTLP gRPC to Alloy.

``init_telemetry`` is a no-op (NoOp/Proxy provider stays global) when
``OTEL_SDK_DISABLED=true`` or ``OTEL_EXPORTER_OTLP_ENDPOINT`` is empty, so the bots start and
work without a collector. Export is asynchronous (``BatchSpanProcessor``); every exported span
goes through :class:`ScrubbingSpanExporter`, which applies the Sentry scrubbing rules
(:func:`app.core.observability.sentry.scrub`) to span/event names, span/event/link attributes
and the status description. Errors stay with ``sentry_sdk`` (no ``SentrySpanProcessor``).

Sampling is ``ParentBased(TraceIdRatioBased(OTEL_TRACES_SAMPLER_ARG))``: with a ratio < 1 the
logs still carry ``trace_id`` of unsampled traces, which are absent in Tempo.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from opentelemetry import trace
from opentelemetry.attributes import BoundedAttributes
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.sdk.util import BoundedList
from opentelemetry.trace import Link, Status
from opentelemetry.util.types import Attributes

from app.core.observability.sentry import FILTERED, is_secret_key, resolve_release, scrub

if TYPE_CHECKING:
    from app.core.config import Settings

__all__ = [
    "OTLP_EXPORT_TIMEOUT_SECONDS",
    "ScrubbingSpanExporter",
    "current_span_ids",
    "get_tracer",
    "get_tracer_provider",
    "init_telemetry",
    "scrub_attributes",
    "shutdown_telemetry",
]

# Per-request deadline of the OTLP exporter (retries included): bounds export/flush/shutdown.
OTLP_EXPORT_TIMEOUT_SECONDS = 2.0

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


def _bounded_attributes(attributes: Attributes, dropped: int) -> BoundedAttributes:
    """Scrubbed copy that keeps the ``dropped`` counter of the original span."""
    bounded = BoundedAttributes(maxlen=None, attributes=scrub_attributes(attributes))
    bounded.dropped = dropped
    return bounded


def _bounded(items: Sequence[Any], dropped: int) -> BoundedList[Any]:
    bounded = BoundedList.from_seq(None, items)
    bounded.dropped = dropped
    return bounded


def _scrub_span(span: ReadableSpan) -> ReadableSpan:
    status = span.status
    if status.description:
        status = Status(status.status_code, scrub(status.description))
    events = [
        Event(scrub(e.name), scrub_attributes(e.attributes), e.timestamp) for e in span.events
    ]
    links = [Link(link.context, scrub_attributes(link.attributes)) for link in span.links]
    return ReadableSpan(
        name=scrub(span.name),
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=_bounded_attributes(span.attributes, span.dropped_attributes),
        events=_bounded(events, span.dropped_events),
        links=_bounded(links, span.dropped_links),
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


def _instrument_sqlalchemy(provider: TracerProvider) -> None:
    """Official global mode: wraps ``sqlalchemy.ext.asyncio.create_async_engine``, so every
    engine built afterwards (``build_engine`` calls it through the module) gets its own
    ``EngineTracer``. ``instrument(engine=...)`` would cover only the first engine (the
    instrumentor is a singleton). 0.66b2 declares ``sqlalchemy < 2.1``: the dependency check
    is skipped (spans verified on SQLAlchemy 2.1.8).
    """
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    instrumentor = SQLAlchemyInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument(tracer_provider=provider, skip_dep_check=True)


def _uninstrument_sqlalchemy() -> None:
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    instrumentor = SQLAlchemyInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()


def init_telemetry(
    settings: Settings,
    *,
    service_name: str,
    set_global: bool = True,
    exporter: SpanExporter | None = None,
) -> bool:
    """Configure tracing; return whether it is enabled (False = NoOp, nothing exported).

    Idempotent: a repeated call keeps the existing provider (no second export thread).
    Call before building engines/brokers: they capture the provider when created.
    """
    global _provider
    if _provider is not None:
        return True
    obs = settings.observability
    endpoint = obs.otel_exporter_otlp_endpoint
    if obs.otel_sdk_disabled or not endpoint:
        return False
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            insecure=urlsplit(endpoint).scheme != "https",
            timeout=OTLP_EXPORT_TIMEOUT_SECONDS,
        )
    provider = build_tracer_provider(settings, service_name=service_name)
    provider.add_span_processor(BatchSpanProcessor(ScrubbingSpanExporter(exporter)))
    _provider = provider
    _instrument_sqlalchemy(provider)
    if set_global:
        trace.set_tracer_provider(provider)
    return True


def shutdown_telemetry(timeout_millis: int = 2000) -> None:
    """Flush pending spans and stop the exporter (no-op when tracing is disabled).

    Blocking (up to ~``timeout_millis`` + exporter deadline): call it from async code via
    ``asyncio.to_thread``.
    """
    global _provider
    if _provider is None:
        return
    _uninstrument_sqlalchemy()
    _provider.force_flush(timeout_millis)
    _provider.shutdown()
    _provider = None
