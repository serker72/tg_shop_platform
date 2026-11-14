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
from faststream import BaseMiddleware, PublishCommand
from faststream.message import StreamMessage
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from app.core.logging import bind_trace_id, clear_trace_id, get_trace_id
from app.core.telemetry import get_tracer

__all__ = [
    "TRACE_ID_HEADER",
    "UPDATE_SPAN_NAME",
    "AiogramTracingMiddleware",
    "TraceIdFallbackMiddleware",
]

UPDATE_SPAN_NAME = "aiogram.update"
TRACE_ID_HEADER = "trace_id"


def _no_active_span() -> bool:
    return not trace.get_current_span().get_span_context().is_valid


class TraceIdFallbackMiddleware(BaseMiddleware[PublishCommand, Any]):
    """Log correlation over RabbitMQ when OTel tracing is off (NoOp provider).

    Installed after ``RabbitTelemetryMiddleware``. With a valid span (tracing on) it does
    nothing: ``traceparent`` carries the context. Otherwise publish adds the bound trace id
    (or a fresh uuid hex) as the ``trace_id`` header and consume binds it for the handler.
    """

    async def publish_scope(
        self,
        call_next: Callable[[PublishCommand], Awaitable[Any]],
        cmd: PublishCommand,
    ) -> Any:
        if _no_active_span() and not cmd.headers.get(TRACE_ID_HEADER):
            cmd.headers = {**cmd.headers, TRACE_ID_HEADER: get_trace_id() or uuid.uuid4().hex}
        return await call_next(cmd)

    async def consume_scope(
        self,
        call_next: Callable[[Any], Awaitable[Any]],
        msg: StreamMessage[Any],
    ) -> Any:
        raw = msg.headers.get(TRACE_ID_HEADER)
        if not (isinstance(raw, str) and raw) or not _no_active_span():
            return await call_next(msg)
        token = bind_trace_id(raw)
        try:
            return await call_next(msg)
        finally:
            clear_trace_id(token)


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
