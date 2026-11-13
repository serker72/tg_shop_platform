"""Traceability guard: ``req(...)`` markers of the test suite <-> :mod:`tests.traceability`.

Markers are collected by a ``pytest --collect-only --req-report`` subprocess (the same plugin
the report uses), so the guard sees exactly what ``make test-report`` will see, including
integration tests that are not run here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.traceability import PROMTOOL_REQS, REQUIREMENTS, STATUSES

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def collected(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    out = tmp_path_factory.mktemp("traceability") / "collect.json"
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [*cmd, f"--req-report={out}", "tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    tests: list[dict[str, Any]] = json.loads(out.read_text("utf-8"))["tests"]
    return tests


def test_registry_is_consistent() -> None:
    for key, req in REQUIREMENTS.items():
        assert key == req.id
        assert req.status in STATUSES, req.id
        assert req.title and req.customer_text, req.id
        assert "\n" not in req.customer_text, req.id
        if req.status != "auto":
            assert req.scope_note, f"{req.id}: manual/deferred needs scope_note"
    assert set(PROMTOOL_REQS) <= REQUIREMENTS.keys()


def test_marker_codes_exist(collected: list[dict[str, Any]]) -> None:
    unknown = {(code, t["nodeid"]) for t in collected for code in t["reqs"]} - {
        (code, t["nodeid"]) for t in collected for code in t["reqs"] if code in REQUIREMENTS
    }
    assert not unknown, f"req codes missing in tests/traceability.py: {sorted(unknown)}"


def test_auto_requirements_are_covered(collected: list[dict[str, Any]]) -> None:
    covered = {code for t in collected for code in t["reqs"]}
    missing = [r.id for r in REQUIREMENTS.values() if r.status == "auto" and r.id not in covered]
    assert not missing, f"requirements without tests: {missing}"


def test_deferred_requirements_have_no_tests(collected: list[dict[str, Any]]) -> None:
    deferred = {r.id for r in REQUIREMENTS.values() if r.status == "deferred"}
    marked = {code for t in collected for code in t["reqs"]} & deferred
    assert not marked, f"deferred requirements must not be marked: {sorted(marked)}"
