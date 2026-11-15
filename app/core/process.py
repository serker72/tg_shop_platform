"""Long-running process helper of the bot entrypoints (``run_service``).

Contract for process components (scheduler of ``bot_worker``, Master Bot polling, bot
runners): a component is ``Callable[[ServiceContext], AsyncContextManager]``; it starts its
work on enter and stops it on exit (LIFO, after SIGTERM/SIGINT). Every long-lived asyncio
task of a component must be passed to ``ctx.supervisor.watch(task)``: a task that ends on
its own (exception, return or foreign cancel) before shutdown is logged as ``critical`` and
stops the process with exit code 1, so the container restart policy restarts it instead of
leaving a half-alive process with green health checks.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:
    from dishka import AsyncContainer

    from app.core.config import Settings
    from app.core.observability.metrics import AppMetrics

#: Exit code of a process stopped because a supervised background task died.
BACKGROUND_FAILURE_EXIT_CODE: Final = 1

_log = structlog.get_logger(__name__)


class TaskSupervisor:
    """Watches the background tasks of a process; an unexpected end requests shutdown."""

    def __init__(self) -> None:
        self.stop_requested = asyncio.Event()
        self.failed: list[str] = []
        self._stopping = False

    def watch(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        task.add_done_callback(self._on_done)
        return task

    def begin_shutdown(self) -> None:
        """From now on finished tasks are expected (components are being stopped)."""
        self._stopping = True

    @property
    def exit_code(self) -> int:
        return BACKGROUND_FAILURE_EXIT_CODE if self.failed else 0

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        if self._stopping:
            return
        name = task.get_name()
        error: BaseException | None = None
        if task.cancelled():
            reason = "cancelled"
        else:
            error = task.exception()
            reason = "exception" if error is not None else "returned"
        _log.critical(
            "background_task_died",
            task=name,
            reason=reason,
            error=type(error).__name__ if error is not None else None,
            exc_info=error,
        )
        self.failed.append(name)
        self.stop_requested.set()


@dataclass(frozen=True, slots=True)
class ServiceContext:
    """What a service component gets from :func:`run_service`."""

    name: str
    settings: Settings
    container: AsyncContainer
    metrics: AppMetrics
    supervisor: TaskSupervisor = field(default_factory=TaskSupervisor)


#: Process component: entered after the observability server, exited (LIFO) on shutdown.
ServiceComponent = Callable[[ServiceContext], AbstractAsyncContextManager[object]]


async def wait_for_shutdown(stop: asyncio.Event | None = None) -> signal.Signals | None:
    """Block until SIGTERM/SIGINT (returned) or ``stop`` is set (``None`` returned)."""
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    received: list[signal.Signals] = []

    def _handle(sig: signal.Signals) -> None:
        received.append(sig)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle, sig)
    waiters = [asyncio.ensure_future(stop_event.wait())]
    if stop is not None:
        waiters.append(asyncio.ensure_future(stop.wait()))
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
    return received[0] if received else None


async def serve_components(context: ServiceContext, components: Sequence[ServiceComponent]) -> int:
    """Enter ``components``, wait for a signal or a dead background task, stop them (LIFO).

    Returns the process exit code: ``0`` after a signal, ``1`` if a supervised task died.
    """
    supervisor = context.supervisor
    async with AsyncExitStack() as stack:
        # Safety net only: registered first, so on LIFO exit it runs LAST - covers an
        # exception while entering components, not the regular shutdown below.
        stack.callback(supervisor.begin_shutdown)
        for component_factory in components:
            await stack.enter_async_context(component_factory(context))
        print(f"{context.name} started", flush=True)
        sig = await wait_for_shutdown(supervisor.stop_requested)
        # Before the components exit: the tasks they cancel and await are expected to end.
        supervisor.begin_shutdown()
        if sig is not None:
            print(f"{context.name} received {sig.name}, shutting down", flush=True)
        else:
            _log.critical("service_stopping_after_task_failure", tasks=supervisor.failed)
    return supervisor.exit_code


async def run_service(
    name: str,
    *,
    components: Sequence[ServiceComponent] = (),
    heartbeats: Mapping[str, bool] | None = None,
) -> None:
    """Service with Sentry, the health/metrics HTTP server and pluggable components.

    Serves ``/health/live``, ``/health/ready`` and ``/metrics`` until SIGTERM/SIGINT.
    ``heartbeats`` (component -> required) are declared before the health checker is built;
    ``components`` are async context managers entered in order after the HTTP server and
    exited in reverse order on shutdown (graceful stop of loops/brokers). A dead supervised
    background task stops the process with ``SystemExit(1)`` after the regular cleanup.
    """
    import sentry_sdk
    from dishka import make_async_container
    from redis.asyncio import Redis

    from app.core.config import Settings, get_settings
    from app.core.logging import configure_logging
    from app.core.observability.health import HealthChecker, HeartbeatRegistry
    from app.core.observability.http_server import create_app, serve_observability
    from app.core.observability.metrics import (
        AppMetrics,
        register_component_metrics,
        set_build_info,
    )
    from app.core.observability.providers import ObservabilityProvider
    from app.core.observability.sentry import init_sentry
    from app.core.telemetry import init_telemetry, shutdown_telemetry
    from app.modules.bot_manager.metrics import OutageState
    from app.modules.bot_manager.proxy_health import OutageMonitor
    from app.modules.logging.ioc import LoggingProvider
    from app.shared_kernel.infrastructure.providers import (
        DatabaseProvider,
        RedisProvider,
    )

    settings = get_settings()
    # ``Settings`` has no log-level option yet: INFO (the ``configure_logging`` default).
    configure_logging()
    sentry_enabled = init_sentry(settings, component=name)
    # Before the container: the engine/broker capture the tracer provider when built.
    init_telemetry(settings, service_name=name)
    container = make_async_container(
        DatabaseProvider(),
        RedisProvider(),
        LoggingProvider(),
        ObservabilityProvider(),
        context={Settings: settings},
    )
    exit_code = 0
    try:
        if heartbeats:
            registry = await container.get(HeartbeatRegistry)
            for component, required in heartbeats.items():
                registry.register(component, required=required)
        checker = await container.get(HealthChecker)
        metrics = await container.get(AppMetrics)
        set_build_info(metrics, component=name)
        context = ServiceContext(name=name, settings=settings, container=container, metrics=metrics)
        outage_state: OutageState | None = None
        obs = settings.observability
        async with AsyncExitStack() as stack:
            if name == "master_bot":
                # ``telegram_outage``: background poll of ``proxy:outage`` -> in-memory cache.
                outage_state = OutageState()
                monitor = OutageMonitor(await container.get(Redis), outage_state)
                context.supervisor.watch(await stack.enter_async_context(monitor.running()))
            register_component_metrics(metrics, component=name, outage_state=outage_state)
            await stack.enter_async_context(
                serve_observability(
                    create_app(
                        checker,
                        metrics.registry,
                        ready_cache_ttl=obs.ready_cache_ttl_seconds,
                        component=name,
                        environment=obs.sentry_environment or settings.project_environment,
                    ),
                    host=obs.http_host,
                    port=obs.http_port,
                )
            )
            exit_code = await serve_components(context, components)
            # The outage monitor is cancelled after this point: expected, not a failure.
            context.supervisor.begin_shutdown()
    finally:
        await container.close()
        # Flush/shutdown blocks (exporter deadline): keep the event loop free.
        await asyncio.to_thread(shutdown_telemetry)
        if sentry_enabled:
            sentry_sdk.flush(timeout=2)
    if exit_code:
        raise SystemExit(exit_code)


async def run_placeholder(name: str) -> None:
    """Print a startup banner and idle until a termination signal arrives."""
    print(f"{name} started (placeholder until Stage 3/4)", flush=True)
    sig = await wait_for_shutdown()
    print(f"{name} received {sig.name if sig else 'stop'}, shutting down", flush=True)
