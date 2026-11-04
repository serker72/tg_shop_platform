"""Minimal long-running process helper used by placeholder entrypoints."""

import asyncio
import signal


async def wait_for_shutdown() -> signal.Signals:
    """Block until SIGTERM or SIGINT is received and return the received signal."""
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    received: list[signal.Signals] = []

    def _handle(sig: signal.Signals) -> None:
        received.append(sig)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle, sig)
    try:
        await stop_event.wait()
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
    return received[0]


async def run_placeholder(name: str) -> None:
    """Print a startup banner and idle until a termination signal arrives."""
    print(f"{name} started (placeholder until Stage 3/4)", flush=True)
    sig = await wait_for_shutdown()
    print(f"{name} received {sig.name}, shutting down", flush=True)
