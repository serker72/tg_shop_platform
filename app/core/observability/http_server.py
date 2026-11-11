"""aiohttp server exposing ``/health/live``, ``/health/ready``, ``/metrics`` and ``/version``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiohttp import web
from prometheus_client import CollectorRegistry

from app.core.observability.health import HealthChecker, HealthReportCache
from app.core.observability.metrics import METRICS_CONTENT_TYPE, render_metrics
from app.core.version import VersionInfo, get_version_info

__all__ = [
    "READY_CACHE_KEY",
    "REGISTRY_KEY",
    "VERSION_KEY",
    "create_app",
    "serve_observability",
]

READY_CACHE_KEY = web.AppKey("health_report_cache", HealthReportCache)
REGISTRY_KEY = web.AppKey("metrics_registry", CollectorRegistry)
#: Ready-to-serve ``/version`` payload (computed once at startup).
VERSION_KEY = web.AppKey("version_payload", dict)


async def _live(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _ready(request: web.Request) -> web.Response:
    report = await request.app[READY_CACHE_KEY].get()
    return web.json_response(report.as_dict(), status=200 if report.ready else 503)


async def _metrics(request: web.Request) -> web.Response:
    body = render_metrics(request.app[REGISTRY_KEY])
    return web.Response(body=body, headers={"Content-Type": METRICS_CONTENT_TYPE})


async def _version(request: web.Request) -> web.Response:
    return web.json_response(request.app[VERSION_KEY])


def create_app(
    checker: HealthChecker,
    registry: CollectorRegistry,
    *,
    ready_cache_ttl: float = 1.0,
    component: str = "unknown",
    environment: str = "unknown",
    version_info: VersionInfo | None = None,
) -> web.Application:
    """Build the app; ``/health/ready`` reuses one report for ``ready_cache_ttl`` seconds.

    ``/version`` returns ``version``/``commit``/``build_time`` (default: the process version)
    plus ``component`` and ``environment``; no secrets.
    """
    info = version_info if version_info is not None else get_version_info()
    app = web.Application()
    app[READY_CACHE_KEY] = HealthReportCache(checker, ttl=ready_cache_ttl)
    app[REGISTRY_KEY] = registry
    app[VERSION_KEY] = {**info.as_dict(), "component": component, "environment": environment}
    app.router.add_get("/health/live", _live)
    app.router.add_get("/health/ready", _ready)
    app.router.add_get("/metrics", _metrics)
    app.router.add_get("/version", _version)
    return app


@asynccontextmanager
async def serve_observability(
    app: web.Application, *, host: str, port: int
) -> AsyncIterator[web.AppRunner]:
    """Serve ``app`` on ``host:port`` for the duration of the context."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
        yield runner
    finally:
        await runner.cleanup()
