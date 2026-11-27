"""Single factory of the FastStream RabbitMQ broker.

The broker gets an explicit stdlib ``logger=`` that propagates to the root logger, so its
records go through the shared structlog JSON pipeline (``configure_logging``) instead of
FastStream's default ``faststream.access.rabbit`` logger (own stdout handler,
``propagate=False``, plain-text format).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from faststream.rabbit import Channel, RabbitBroker
from faststream.rabbit.opentelemetry import RabbitTelemetryMiddleware

from app.core.config import Settings
from app.core.observability.tracing import TraceIdFallbackMiddleware
from app.core.telemetry import get_tracer_provider

__all__ = ["BROKER_LOGGER_NAME", "broker_logger", "build_rabbit_broker"]

BROKER_LOGGER_NAME = "app.broker"


def broker_logger() -> logging.Logger:
    """Stdlib logger of the broker: no own handlers, propagates to the root pipeline."""
    logger = logging.getLogger(BROKER_LOGGER_NAME)
    logger.propagate = True
    return logger


#: Time the broker waits for in-flight handlers on stop; compose ``stop_grace_period`` of the
#: bot services (30 s) must stay above it plus telemetry/Sentry flush.
BROKER_GRACEFUL_TIMEOUT_SECONDS = 15.0


def build_rabbit_broker(
    settings: Settings,
    *,
    on_close: Callable[[object, BaseException | None], Awaitable[None] | None] | None = None,
    on_reconnected: Callable[[object], None] | None = None,
) -> RabbitBroker:
    """Broker on ``settings.rabbitmq_url`` with OTel spans and the JSON logger.

    ``RabbitTelemetryMiddleware`` (``faststream[otel]``) creates publish/process spans and
    propagates the context in the W3C ``traceparent`` header; ``TraceIdFallbackMiddleware``
    keeps the ``trace_id`` header for log correlation when tracing is off. Build the broker after
    ``init_telemetry``: the provider is captured here.
    """
    return RabbitBroker(
        settings.rabbitmq_url,
        logger=broker_logger(),
        default_channel=Channel(publisher_confirms=True, on_return_raises=True),
        graceful_timeout=BROKER_GRACEFUL_TIMEOUT_SECONDS,
        on_close=(on_close,) if on_close is not None else None,
        on_reconnected=(on_reconnected,) if on_reconnected is not None else None,
        middlewares=[
            RabbitTelemetryMiddleware(tracer_provider=get_tracer_provider()),
            # Only when tracing is off: trace_id header for log correlation.
            TraceIdFallbackMiddleware,
        ],
    )
