import asyncio
import os
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, nullcontext, suppress
from types import SimpleNamespace
from typing import Any, cast

import pytest
from structlog.testing import capture_logs

from app.core.process import (
    BACKGROUND_FAILURE_EXIT_CODE,
    ServiceContext,
    TaskSupervisor,
    serve_components,
    wait_for_shutdown,
)

#: Overall deadline of a condition wait (no fixed sleeps in these tests).
WAIT_TIMEOUT = 5.0


async def wait_until(condition: Callable[[], bool], deadline: float = WAIT_TIMEOUT) -> None:
    """Poll ``condition`` until true; ``TimeoutError`` after ``deadline`` seconds."""

    async def poll() -> None:
        while True:
            if condition():
                return
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), deadline)


def sigterm_when(condition: Callable[[], bool] = lambda: True) -> asyncio.Task[None]:
    """Send SIGTERM once ``wait_for_shutdown`` installed its handler and ``condition`` holds.

    The original handler is captured synchronously, before the waiter can install its own.
    """
    original = signal.getsignal(signal.SIGTERM)

    async def run() -> None:
        await wait_until(lambda: signal.getsignal(signal.SIGTERM) is not original and condition())
        os.kill(os.getpid(), signal.SIGTERM)

    return asyncio.create_task(run())


async def test_wait_for_shutdown_returns_on_sigterm() -> None:
    killer = sigterm_when()

    received = await asyncio.wait_for(wait_for_shutdown(), timeout=WAIT_TIMEOUT)

    await killer
    assert received is signal.SIGTERM


async def test_wait_for_shutdown_returns_none_on_stop_event() -> None:
    stop = asyncio.Event()
    asyncio.get_running_loop().call_soon(stop.set)
    assert await asyncio.wait_for(wait_for_shutdown(stop), timeout=WAIT_TIMEOUT) is None


def _context() -> ServiceContext:
    return ServiceContext(
        name="svc",
        settings=cast(Any, SimpleNamespace()),
        container=cast(Any, SimpleNamespace()),
        metrics=cast(Any, SimpleNamespace()),
    )


async def test_dead_background_task_stops_process_with_nonzero_code() -> None:
    events: list[str] = []

    async def crash() -> None:
        raise RuntimeError("boom")

    @asynccontextmanager
    async def component(ctx: ServiceContext) -> AsyncIterator[None]:
        task = ctx.supervisor.watch(asyncio.create_task(crash(), name="crasher"))
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")
            task.cancel()

    ctx = _context()
    code = await asyncio.wait_for(serve_components(ctx, [component]), timeout=WAIT_TIMEOUT)
    assert code == BACKGROUND_FAILURE_EXIT_CODE
    assert ctx.supervisor.failed == ["crasher"]
    assert events == ["enter", "exit"]


async def test_components_exit_lifo_on_signal_without_failure() -> None:
    events: list[str] = []

    def make(name: str) -> Any:
        @asynccontextmanager
        async def component(ctx: ServiceContext) -> AsyncIterator[None]:
            task = ctx.supervisor.watch(asyncio.create_task(asyncio.sleep(3600), name=name))
            events.append(f"enter:{name}")
            try:
                yield
            finally:
                # Like real components: cancel AND await (done callbacks fire here).
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                events.append(f"exit:{name}")

        return component

    killer = sigterm_when(lambda: len(events) == 2)
    ctx = _context()
    with capture_logs() as logs:
        code = await asyncio.wait_for(
            serve_components(ctx, [make("a"), make("b")]), timeout=WAIT_TIMEOUT
        )
    await killer
    assert code == 0
    assert ctx.supervisor.failed == []
    assert not [e for e in logs if e["event"] == "background_task_died"]
    assert events == ["enter:a", "enter:b", "exit:b", "exit:a"]


@pytest.mark.parametrize("outcome", ["return", "cancel"])
async def test_supervisor_flags_returned_or_foreign_cancelled_task(outcome: str) -> None:
    supervisor = TaskSupervisor()
    task = supervisor.watch(asyncio.create_task(asyncio.sleep(0 if outcome == "return" else 10)))
    if outcome == "cancel":
        await asyncio.sleep(0)
        task.cancel()
    with pytest.raises(asyncio.CancelledError) if outcome == "cancel" else nullcontext():
        await task
    await wait_until(supervisor.stop_requested.is_set)
    assert supervisor.exit_code == BACKGROUND_FAILURE_EXIT_CODE
