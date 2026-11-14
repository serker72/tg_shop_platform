"""OpenTelemetry end-to-end smoke: span -> SQL -> RabbitMQ publish/consume -> Sentry error.

Run inside the master_bot container (``make otel-smoke``: the file is piped to ``python -``).
Prints only ``TRACE_ID <32 hex>`` and ``SENTRY_EVENT_ID <hex>`` - no settings or secrets.
Then: Grafana Explore -> Tempo -> trace id; Loki ``{service=~".+"} | trace_id="<id>"``;
urgentry issue "otel smoke forced error" with the same ``contexts.trace.trace_id``.
"""

from __future__ import annotations

import asyncio

QUEUE = "otel-smoke-q"


async def main() -> int:
    import sentry_sdk
    from faststream.rabbit import RabbitQueue
    from sqlalchemy import text

    from app.core.config import get_settings
    from app.core.logging import configure_logging, get_logger
    from app.core.observability.broker import build_rabbit_broker
    from app.core.observability.sentry import init_sentry
    from app.core.telemetry import get_tracer, init_telemetry, shutdown_telemetry
    from app.shared_kernel.infrastructure.database import build_engine

    settings = get_settings()
    configure_logging()
    if not init_telemetry(settings, service_name="master_bot"):
        print("ERROR: tracing is disabled (OTEL_EXPORTER_OTLP_ENDPOINT empty or OTEL_SDK_DISABLED)")
        return 1
    sentry_on = init_sentry(settings, component="otel_smoke")
    log = get_logger("otel_smoke")
    engine = build_engine(settings)
    broker = build_rabbit_broker(settings)
    consumed = asyncio.Event()

    @broker.subscriber(RabbitQueue(QUEUE, auto_delete=True))
    async def handle(message: str) -> None:
        async with engine.connect() as conn:
            await conn.execute(text("select 2"))
        log.info("otel smoke consumed")
        consumed.set()

    event_id: str | None = None
    try:
        async with broker:
            await broker.start()
            with get_tracer("otel_smoke").start_as_current_span("otel.smoke") as span:
                async with engine.connect() as conn:
                    await conn.execute(text("select 1"))
                log.info("otel smoke start")
                await broker.publish("ping", queue=QUEUE)
                await asyncio.wait_for(consumed.wait(), 10)
                try:
                    raise RuntimeError("otel smoke forced error")
                except RuntimeError as exc:
                    log.warning("otel smoke error captured")
                    if sentry_on:
                        event_id = sentry_sdk.capture_exception(exc)
                trace_id = format(span.get_span_context().trace_id, "032x")
    finally:
        await engine.dispose()
        sentry_sdk.flush(5)
        shutdown_telemetry()
    print("TRACE_ID", trace_id, flush=True)
    print("SENTRY_EVENT_ID", event_id or "- (sentry disabled)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
