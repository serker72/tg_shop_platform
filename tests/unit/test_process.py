import asyncio
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.core.process import (
    BACKGROUND_FAILURE_EXIT_CODE,
    ServiceContext,
    TaskSupervisor,
    serve_components,
    wait_for_shutdown,
)


async def test_wait_for_shutdown_returns_on_sigterm() -> None:
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)

    received = await asyncio.wait_for(wait_for_shutdown(), timeout=2)

    assert received is signal.SIGTERM


async def test_wait_for_shutdown_returns_none_on_stop_event() -> None:
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)
    assert await asyncio.wait_for(wait_for_shutdown(stop), timeout=2) is None


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
        await asyncio.sleep(0.05)
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
    code = await asyncio.wait_for(serve_components(ctx, [component]), timeout=5)
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
                task.cancel()  # expected cancel on shutdown - not a failure
                events.append(f"exit:{name}")

        return component

    asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
    ctx = _context()
    code = await asyncio.wait_for(serve_components(ctx, [make("a"), make("b")]), timeout=5)
    await asyncio.sleep(0)
    assert code == 0
    assert ctx.supervisor.failed == []
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
    await asyncio.sleep(0)
    assert supervisor.stop_requested.is_set()
    assert supervisor.exit_code == BACKGROUND_FAILURE_EXIT_CODE
