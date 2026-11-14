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


async def run_service(name: str) -> None:
    """Placeholder service with Sentry and the health/metrics HTTP server.

    Serves ``/health/live``, ``/health/ready`` and ``/metrics`` until SIGTERM/SIGINT; the
    bots themselves arrive in milestone II.
    """
    import sentry_sdk
    from dishka import make_async_container

    from app.core.config import Settings, get_settings
    from app.core.logging import configure_logging
    from app.core.observability.health import HealthChecker
    from app.core.observability.http_server import create_app, serve_observability
    from app.core.observability.metrics import AppMetrics, set_build_info
    from app.core.observability.providers import ObservabilityProvider
    from app.core.observability.sentry import init_sentry
    from app.core.telemetry import init_telemetry, shutdown_telemetry
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
        checker = await container.get(HealthChecker)
        metrics = await container.get(AppMetrics)
        set_build_info(metrics, component=name)
        obs = settings.observability
        async with serve_observability(
            create_app(
                checker,
                metrics.registry,
                ready_cache_ttl=obs.ready_cache_ttl_seconds,
                component=name,
                environment=obs.sentry_environment or settings.project_environment,
            ),
            host=obs.http_host,
            port=obs.http_port,
        ):
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
