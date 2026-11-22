"""RabbitMQ publisher for bot control commands."""

from __future__ import annotations

from typing import Any

from faststream_outbox import OutboxBroker
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bot_manager.commands import BOT_COMMAND_EXCHANGE, BotCommand
from app.shared_kernel.infrastructure.outbox import append_outbox


class BotCommandPublisher:
    def __init__(self, broker: Any) -> None:
        self._broker = broker
        self.supports_transactional = isinstance(broker, OutboxBroker)

    async def publish(self, command: BotCommand) -> Any:
        # Legacy direct transport path retained for non-transactional callers.
        return await self._broker.publish(
            command.model_dump(mode="json"), exchange=BOT_COMMAND_EXCHANGE
        )

    async def publish_transactional(
        self,
        command: BotCommand,
        session: AsyncSession,
        *,
        traceparent: str | None = None,
    ) -> Any:
        """Append to the shared outbox in the caller's open transaction."""
        headers = {"content-type": "application/json", "x-command-type": command.kind}
        if traceparent:
            headers["traceparent"] = traceparent
        return await append_outbox(
            self._broker,
            session,
            payload=command.model_dump(mode="json"),
            headers=headers,
            idempotency_key=str(command.idempotency_key),
        )


__all__ = ["BotCommandPublisher"]
