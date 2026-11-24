"""Commands exchanged between Master Bot and bot workers."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from faststream.rabbit import ExchangeType, RabbitExchange
from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictInt

BOT_COMMAND_EXCHANGE = "bot.commands"
BOT_COMMAND_FANOUT = RabbitExchange(BOT_COMMAND_EXCHANGE, type=ExchangeType.FANOUT, durable=True)
BotCommandKind = Literal["START_BOT", "STOP_BOT", "RESTART_BOT"]


class BotCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Stable business identity travels in the transport envelope, not as a schema column.
    command_id: UUID
    idempotency_key: UUID
    kind: BotCommandKind
    bot_id: UUID
    issued_at: AwareDatetime
    issued_by: StrictInt


__all__ = ["BOT_COMMAND_EXCHANGE", "BotCommand", "BotCommandKind"]
