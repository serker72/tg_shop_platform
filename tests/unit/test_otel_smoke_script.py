"""ops/scripts/otel_smoke.py imports without side effects and exposes an async ``main``."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "otel_smoke.py"


def test_otel_smoke_imports_without_running() -> None:
    spec = importlib.util.spec_from_file_location("otel_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert asyncio.iscoroutinefunction(module.main)
    assert module.QUEUE == "otel-smoke-q"


def test_otel_smoke_prints_no_settings() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    prints = [line for line in source.splitlines() if "print(" in line]
    assert prints and all("settings" not in line and "dsn" not in line.lower() for line in prints)
