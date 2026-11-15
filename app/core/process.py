"""Long-running process helper of the bot entrypoints (``run_service``)."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dishka import AsyncContainer

    from app.core.config import Settings
    from app.core.observability.metrics import AppMetrics


@dataclass(frozen=True, slots=True)
class ServiceContext:
    """What a service component gets from :func:`run_service`."""

    name: str
    settings: Settings
    container: AsyncContainer
    metrics: AppMetrics


#: Process component: entered after the observability server, exited (LIFO) on shutdown.
ServiceComponent = Callable[[ServiceContext], AbstractAsyncContextManager[object]]


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
    exited in reverse order on shutdown (graceful stop of loops/brokers).
    """
    from contextlib import AsyncExitStack

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
    try:
        if heartbeats:
            registry = await container.get(HeartbeatRegistry)
            for component, required in heartbeats.items():
                registry.register(component, required=required)
        checker = await container.get(HealthChecker)
        metrics = await container.get(AppMetrics)
        set_build_info(metrics, component=name)
        outage_state: OutageState | None = None
        obs = settings.observability
        async with AsyncExitStack() as stack:
            if name == "master_bot":
                # ``telegram_outage``: background poll of ``proxy:outage`` -> in-memory cache.
                outage_state = OutageState()
                monitor = OutageMonitor(await container.get(Redis), outage_state)
                await stack.enter_async_context(monitor.running())
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
            context = ServiceContext(
                name=name, settings=settings, container=container, metrics=metrics
            )
            for component_factory in components:
                await stack.enter_async_context(component_factory(context))
            await run_placeholder(name)
    finally:
        await container.close()
        # Flush/shutdown blocks (exporter deadline): keep the event loop free.
        await asyncio.to_thread(shutdown_telemetry)
        if sentry_enabled:
            sentry_sdk.flush(timeout=2)


async def run_placeholder(name: str) -> None:
    """Print a startup banner and idle until a termination signal arrives."""
    print(f"{name} started (placeholder until Stage 3/4)", flush=True)
    sig = await wait_for_shutdown()
    print(f"{name} received {sig.name}, shutting down", flush=True)
