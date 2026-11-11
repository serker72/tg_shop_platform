"""Single factory of the FastStream RabbitMQ broker.

The broker gets an explicit stdlib ``logger=`` that propagates to the root logger, so its
records go through the shared structlog JSON pipeline (``configure_logging``) instead of
FastStream's default ``faststream.access.rabbit`` logger (own stdout handler,
``propagate=False``, plain-text format).
"""

from __future__ import annotations

import logging

from faststream.rabbit import RabbitBroker

from app.core.config import Settings
from app.core.observability.tracing import TraceIdMiddleware

__all__ = ["BROKER_LOGGER_NAME", "broker_logger", "build_rabbit_broker"]

BROKER_LOGGER_NAME = "app.broker"


def broker_logger() -> logging.Logger:
    """Stdlib logger of the broker: no own handlers, propagates to the root pipeline."""
    logger = logging.getLogger(BROKER_LOGGER_NAME)
    logger.propagate = True
    return logger


def build_rabbit_broker(settings: Settings) -> RabbitBroker:
    """Broker on ``settings.rabbitmq_url`` with trace_id propagation and the JSON logger."""
    return RabbitBroker(
        settings.rabbitmq_url,
        logger=broker_logger(),
        middlewares=[TraceIdMiddleware],
    )
