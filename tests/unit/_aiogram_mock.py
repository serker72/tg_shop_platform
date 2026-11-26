"""Small BaseSession double for polling tests."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator
from typing import Any, cast

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetUpdates, TelegramMethod
from aiogram.types import Update


class MockedSession(BaseSession):
    def __init__(self, *responses: list[Update] | Exception) -> None:
        super().__init__()
        self.responses = deque(responses)
        self.closed = False
        self.polling = asyncio.Event()
        self._closed = asyncio.Event()

    async def make_request[T](
        self,
        bot: Bot,
        method: TelegramMethod[T],
        timeout: int | None = None,  # noqa: ASYNC109 - aiogram override
    ) -> T:
        if not isinstance(method, GetUpdates):
            raise AssertionError(f"Unexpected method: {type(method).__name__}")
        if not self.responses:
            self.polling.set()
            await self._closed.wait()
            raise asyncio.CancelledError
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        # The GetUpdates guard ties T to list[Update]; mypy cannot narrow T itself.
        return cast(T, response)

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 - aiogram override
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        raise AssertionError("Polling does not stream content")
        yield b""  # pragma: no cover - preserve the async-generator signature

    async def close(self) -> None:
        self.closed = True
        self._closed.set()
