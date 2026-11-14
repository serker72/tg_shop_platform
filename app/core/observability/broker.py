"""Single factory of the FastStream RabbitMQ broker.

The broker gets an explicit stdlib ``logger=`` that propagates to the root logger, so its
records go through the shared structlog JSON pipeline (``configure_logging``) instead of
FastStream's default ``faststream.access.rabbit`` logger (own stdout handler,
``propagate=False``, plain-text format).
"""

from __future__ import annotations

import logging

from faststream.rabbit import RabbitBroker
from faststream.rabbit.opentelemetry import RabbitTelemetryMiddleware

from app.core.config import Settings
from app.core.telemetry import get_tracer_provider

__all__ = ["BROKER_LOGGER_NAME", "broker_logger", "build_rabbit_broker"]

BROKER_LOGGER_NAME = "app.broker"


def broker_logger() -> logging.Logger:
    """Stdlib logger of the broker: no own handlers, propagates to the root pipeline."""
    logger = logging.getLogger(BROKER_LOGGER_NAME)
    logger.propagate = True
    return logger


def build_rabbit_broker(settings: Settings) -> RabbitBroker:
    """Broker on ``settings.rabbitmq_url`` with OTel spans and the JSON logger.

    ``RabbitTelemetryMiddleware`` (``faststream[otel]``) creates publish/process spans and
    propagates the context in the W3C ``traceparent`` header. Build the broker after
    ``init_telemetry``: the provider is captured here.
    """
    return RabbitBroker(
        settings.rabbitmq_url,
        logger=broker_logger(),
        middlewares=[RabbitTelemetryMiddleware(tracer_provider=get_tracer_provider())],
    )
