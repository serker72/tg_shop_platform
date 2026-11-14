"""OpenTelemetry end-to-end smoke: span -> SQL -> RabbitMQ publish/consume -> Sentry error.

Run inside the master_bot container (``make otel-smoke``: the file is piped to ``python -``;
output is tee'd to PID 1 stdout so that ``docker logs``/Alloy/Loki see the smoke log lines).
Prints only ``TRACE_ID <32 hex>`` and ``SENTRY_EVENT_ID <hex>`` - no settings or secrets.
Then: Grafana Explore -> Tempo -> trace id; Loki ``{service=~".+"} | trace_id="<id>"``;
urgentry issue "otel smoke forced error" with the same ``contexts.trace.trace_id``.

``--deep-error`` (``make otel-smoke DEEP_ERROR=1``) - demo data for manual UI checks: three
read-only SELECTs with JOIN/WHERE (visible as ``db.statement`` in Tempo) and, instead of the
plain error, a 3-level exception chain (``raise ... from ...``) with a 13+ frame stack and
non-secret locals (urgentry issue "OrderProcessingError").

Error spans (dashboard ``tracing-red``: Errors / Error ratio / Failed requests). The exception is
caught inside the script, and ``start_as_current_span`` sets ERROR only for exceptions escaping
the ``with`` block, so it is raised in a dedicated child span (``otel.smoke.failing_operation``;
``process_order`` for ``--deep-error``) marked explicitly via ``record_exception`` +
``set_status(Status(StatusCode.ERROR, <exception type>))``. The root ``otel.smoke`` stays UNSET:
the smoke itself succeeds, the error is intentional and handled. One read-only ``SELECT`` from a
missing table gives an ERROR on the SQLAlchemy CLIENT span (instrumentation ``handle_error``),
i.e. a failed ``master_bot -> postgresql`` service-graph edge
(``traces_service_graph_request_failed_total``).
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from typing import Any

QUEUE = "otel-smoke-q"

# Read-only demo queries over real project tables (no data changes).
DEMO_QUERIES: tuple[str, ...] = (
    "SELECT l.code, l.name, count(ts.id) AS tenants_using "
    "FROM languages AS l LEFT JOIN tenant_settings AS ts ON ts.default_language_code = l.code "
    "WHERE l.is_active AND NOT l.is_deleted GROUP BY l.code, l.name ORDER BY l.code",
    "SELECT t.slug, t.name, ts.is_store_open, ts.default_language_code "
    "FROM tenants AS t JOIN tenant_settings AS ts ON ts.tenant_id = t.id "
    "WHERE t.is_active AND NOT t.is_deleted ORDER BY t.slug LIMIT 10",
    "SELECT s.is_maintenance_mode, s.iam_cache_ttl_seconds, s.file_logs_retention_days "
    "FROM system_settings AS s WHERE s.id = 1",
)

# Read-only query that fails on purpose (missing relation) -> ERROR on the DB client span.
FAILING_QUERY = "SELECT 1 FROM otel_smoke_missing_table"


def mark_span_error(span: Any, exc: BaseException) -> None:
    """Official OTel API: exception event + ERROR status; description = type name only."""
    from opentelemetry.trace import Status, StatusCode

    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))


def raise_forced_error(tracer: Any, deep: bool) -> BaseException:
    """Create the demo exception inside its own child span marked ERROR; returns the exception."""
    name = "process_order" if deep else "otel.smoke.failing_operation"
    with tracer.start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as span:
        exc: BaseException
        if deep:
            exc = build_deep_error()
        else:
            try:
                raise RuntimeError("otel smoke forced error")
            except RuntimeError as err:
                exc = err
        mark_span_error(span, exc)
    return exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="otel_smoke", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--deep-error",
        action="store_true",
        help="demo SELECTs + deep chained exception (manual UI check in urgentry/Tempo)",
    )
    return parser.parse_args(argv)


# --- deep demo error: handle_update -> ... -> validate_stock (fake order flow, no I/O) ---


class StockValidationError(ValueError):
    """Innermost: requested quantity exceeds available stock."""


class ReservationError(RuntimeError):
    """Middle: warehouse reservation failed."""


class OrderProcessingError(RuntimeError):
    """Outermost: update handling failed."""


def validate_stock(sku: str, requested: int, available: int) -> None:
    shortage = requested - available
    if shortage > 0:
        raise StockValidationError(f"sku {sku}: requested {requested}, available {available}")


def fetch_stock_level(sku: str, requested: int, warehouse: str) -> None:
    available = {"SKU-1001": 7, "SKU-2002": 2}.get(sku, 0)
    validate_stock(sku, requested, available)


def check_inventory(sku: str, requested: int, warehouse: str) -> None:
    cache_hit = False  # noqa: F841 - demo local shown in the urgentry frame
    fetch_stock_level(sku, requested, warehouse)


def lock_stock_rows(items: dict[str, int], warehouse: str) -> None:
    lock_mode = "FOR UPDATE SKIP LOCKED"  # noqa: F841 - demo local
    for sku, qty in items.items():
        check_inventory(sku, qty, warehouse)


def allocate_warehouse(items: dict[str, int], region: str) -> None:
    warehouse = f"wh-{region}-01"
    lock_stock_rows(items, warehouse)


def select_supplier(items: dict[str, int], region: str) -> None:
    supplier = "demo-supplier"  # noqa: F841 - demo local
    allocate_warehouse(items, region)


def reserve_items(order_id: int, items: dict[str, int], region: str) -> None:
    reservation_ttl_s = 900  # noqa: F841 - demo local
    select_supplier(items, region)


def compute_shipping(order_id: int, items: dict[str, int], region: str) -> None:
    weight_kg = round(0.35 * sum(items.values()), 2)  # noqa: F841 - demo local
    reserve_items(order_id, items, region)


def apply_discounts(order_id: int, items: dict[str, int], subtotal: int) -> None:
    promo_code = "DEMO10"  # noqa: F841 - demo local
    total = subtotal - subtotal // 10  # noqa: F841 - demo local
    compute_shipping(order_id, items, region="eu")


def price_cart(order_id: int, items: dict[str, int]) -> None:
    prices = {"SKU-1001": 1290, "SKU-2002": 4990}
    subtotal = sum(prices.get(sku, 0) * qty for sku, qty in items.items())
    apply_discounts(order_id, items, subtotal)


def load_cart(order_id: int, user_id: int) -> None:
    items = {"SKU-1001": 1, "SKU-2002": 5}
    price_cart(order_id, items)


def process_order(order_id: int, user_id: int) -> None:
    attempt = 1  # noqa: F841 - demo local
    load_cart(order_id, user_id)


def dispatch_command(command: str, user_id: int) -> None:
    order_id = 424242
    try:
        process_order(order_id, user_id)
    except StockValidationError as exc:
        raise ReservationError(f"order {order_id}: stock reservation failed") from exc


def handle_update(update_id: int, user_id: int) -> None:
    command = "/buy"
    try:
        dispatch_command(command, user_id)
    except ReservationError as exc:
        raise OrderProcessingError(f"update {update_id}: {command} failed") from exc


def build_deep_error() -> OrderProcessingError:
    """Raise and catch the demo chain; returns the outermost exception (tracebacks attached)."""
    try:
        handle_update(update_id=100500, user_id=777)
    except OrderProcessingError as exc:
        return exc
    raise AssertionError("demo chain did not raise")  # pragma: no cover


async def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    import sentry_sdk
    from faststream.rabbit import RabbitQueue
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.core.config import get_settings
    from app.core.logging import configure_logging, get_logger
    from app.core.observability.broker import build_rabbit_broker
    from app.core.observability.sentry import init_sentry
    from app.core.telemetry import get_tracer, init_telemetry, shutdown_telemetry
    from app.shared_kernel.infrastructure.database import build_engine

    settings = get_settings()
    configure_logging()
    if not init_telemetry(settings, service_name="master_bot"):
        print("ERROR: tracing is disabled (OTEL_EXPORTER_OTLP_ENDPOINT empty or OTEL_SDK_DISABLED)")
        return 1
    sentry_on = init_sentry(settings, component="otel_smoke")
    log = get_logger("otel_smoke")
    engine = build_engine(settings)
    broker = build_rabbit_broker(settings)
    consumed = asyncio.Event()

    @broker.subscriber(RabbitQueue(QUEUE, auto_delete=True))
    async def handle(message: str) -> None:
        async with engine.connect() as conn:
            await conn.execute(text("select 2"))
        log.info("otel smoke consumed")
        consumed.set()

    event_id: str | None = None
    try:
        async with broker:
            await broker.start()
            tracer = get_tracer("otel_smoke")
            with tracer.start_as_current_span("otel.smoke") as span:
                async with engine.connect() as conn:
                    await conn.execute(text("select 1"))
                    if args.deep_error:
                        for query in DEMO_QUERIES:
                            await conn.execute(text(query))
                log.info("otel smoke start")
                await broker.publish("ping", queue=QUEUE)
                await asyncio.wait_for(consumed.wait(), 10)
                try:  # separate connection: the failed statement does not touch the one above
                    async with engine.connect() as conn:
                        await conn.execute(text(FAILING_QUERY))
                except DBAPIError:
                    log.info("otel smoke failing select done")
                exc = raise_forced_error(tracer, args.deep_error)
                log.warning("otel smoke error captured")
                if sentry_on:
                    event_id = sentry_sdk.capture_exception(exc)
                trace_id = format(span.get_span_context().trace_id, "032x")
    finally:
        await engine.dispose()
        sentry_sdk.flush(5)
        shutdown_telemetry()
    print("TRACE_ID", trace_id, flush=True)
    print("SENTRY_EVENT_ID", event_id or "- (sentry disabled)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
