"""``trace_id`` propagation: FastStream (RabbitMQ headers) and aiogram (one id per update)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware as AiogramBaseMiddleware
from aiogram.types import TelegramObject
from faststream import BaseMiddleware, PublishCommand
from faststream.message import StreamMessage

from app.core.logging import bind_trace_id, clear_trace_id, get_trace_id

__all__ = ["TRACE_ID_HEADER", "AiogramTraceIdMiddleware", "TraceIdMiddleware"]

TRACE_ID_HEADER = "trace_id"


def _generate() -> str:
    return uuid.uuid4().hex


class TraceIdMiddleware(BaseMiddleware[PublishCommand, Any]):
    """Broker middleware, installed by ``app.core.observability.broker.build_rabbit_broker``.

    Publish: puts the bound trace id (or a fresh one) into the ``trace_id`` header unless
    the caller set it explicitly. Consume: binds the header value (or a fresh id) for the
    handler and restores the previous context afterwards.
    """

    async def publish_scope(
        self,
        call_next: Callable[[PublishCommand], Awaitable[Any]],
        cmd: PublishCommand,
    ) -> Any:
        if not cmd.headers.get(TRACE_ID_HEADER):
            cmd.headers = {**cmd.headers, TRACE_ID_HEADER: get_trace_id() or _generate()}
        return await call_next(cmd)

    async def consume_scope(
        self,
        call_next: Callable[[Any], Awaitable[Any]],
        msg: StreamMessage[Any],
    ) -> Any:
        raw = msg.headers.get(TRACE_ID_HEADER)
        trace_id = raw if isinstance(raw, str) and raw else _generate()
        token = bind_trace_id(trace_id)
        try:
            return await call_next(msg)
        finally:
            clear_trace_id(token)


class AiogramTraceIdMiddleware(AiogramBaseMiddleware):
    """Outer update middleware: a new ``trace_id`` per Telegram update (``data["trace_id"]``)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        trace_id = _generate()
        data["trace_id"] = trace_id
        token = bind_trace_id(trace_id)
        try:
            return await handler(event, data)
        finally:
            clear_trace_id(token)
