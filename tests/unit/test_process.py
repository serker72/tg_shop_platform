import asyncio
import os
import signal

from app.core.process import wait_for_shutdown


async def test_wait_for_shutdown_returns_on_sigterm() -> None:
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)

    received = await asyncio.wait_for(wait_for_shutdown(), timeout=2)

    assert received is signal.SIGTERM
