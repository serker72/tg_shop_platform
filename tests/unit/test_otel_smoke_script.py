"""ops/scripts/otel_smoke.py imports without side effects and exposes an async ``main``."""

from __future__ import annotations

import asyncio
import importlib.util
import re
import traceback
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "otel_smoke.py"


def test_otel_smoke_imports_without_running() -> None:
    spec = importlib.util.spec_from_file_location("otel_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert asyncio.iscoroutinefunction(module.main)
    assert module.QUEUE == "otel-smoke-q"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("otel_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_otel_smoke_deep_error_flag() -> None:
    module = _load()
    assert module.parse_args([]).deep_error is False
    assert module.parse_args(["--deep-error"]).deep_error is True


def test_otel_smoke_deep_error_chain_and_depth() -> None:
    module = _load()
    exc = module.build_deep_error()
    chain = []
    cur: BaseException | None = exc
    while cur is not None:
        chain.append(type(cur).__name__)
        cur = cur.__cause__
    assert chain == ["OrderProcessingError", "ReservationError", "StockValidationError"]
    assert exc.__cause__ is not None and exc.__cause__.__cause__ is not None
    inner = exc.__cause__.__cause__
    frames = traceback.extract_tb(inner.__traceback__)
    assert len(frames) >= 12
    assert frames[-1].name == "validate_stock"


def test_otel_smoke_demo_queries_are_read_only() -> None:
    module = _load()
    assert len(module.DEMO_QUERIES) == 3
    for query in module.DEMO_QUERIES:
        assert query.lstrip().upper().startswith("SELECT ")
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE)\b", query, re.I)


def test_otel_smoke_prints_no_settings() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    prints = [line for line in source.splitlines() if "print(" in line]
    assert prints and all("settings" not in line and "dsn" not in line.lower() for line in prints)
