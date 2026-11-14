"""aiogram update tracing: one OTel root span per Telegram update.

There is no official OpenTelemetry instrumentation for aiogram, hence this outer middleware.
RabbitMQ propagation is W3C ``traceparent`` via FastStream's ``RabbitTelemetryMiddleware``
(see :mod:`app.core.observability.broker`).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware as AiogramBaseMiddleware
from aiogram.types import TelegramObject, Update
from opentelemetry.trace import SpanKind

from app.core.logging import bind_trace_id, clear_trace_id
from app.core.telemetry import get_tracer

__all__ = ["UPDATE_SPAN_NAME", "AiogramTraceIdMiddleware", "AiogramTracingMiddleware"]

UPDATE_SPAN_NAME = "aiogram.update"


def _update_attributes(event: TelegramObject, data: dict[str, Any]) -> dict[str, Any]:
    """Update type/id and bot id only: no user ids, texts or other PII."""
    attributes: dict[str, Any] = {"messaging.system": "telegram"}
    if isinstance(event, Update):
        attributes["telegram.update_id"] = event.update_id
        try:
            attributes["telegram.update_type"] = event.event_type
        except Exception:  # noqa: BLE001, S110 - unknown update type
            pass
    bot = data.get("bot")
    bot_id = getattr(bot, "id", None)
    if isinstance(bot_id, int):
        attributes["telegram.bot_id"] = bot_id
    return attributes


class AiogramTracingMiddleware(AiogramBaseMiddleware):
    """Outer update middleware: root span ``aiogram.update``; ``data["trace_id"]`` (32 hex).

    Exceptions are recorded on the span (status ERROR) and re-raised. Without tracing
    (NoOp provider) a random 32-hex id is bound for the logs instead.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tracer = get_tracer(__name__)
        with tracer.start_as_current_span(
            UPDATE_SPAN_NAME, kind=SpanKind.CONSUMER, attributes=_update_attributes(event, data)
        ) as span:
            ctx = span.get_span_context()
            trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else uuid.uuid4().hex
            data["trace_id"] = trace_id
            token = bind_trace_id(trace_id)
            try:
                return await handler(event, data)
            finally:
                clear_trace_id(token)


# Backwards-compatible name.
AiogramTraceIdMiddleware = AiogramTracingMiddleware
