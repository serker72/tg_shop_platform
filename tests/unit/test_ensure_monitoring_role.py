"""ops/scripts/ensure_monitoring_role.py: password validation before any DB access."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "ensure_monitoring_role.py"


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("ensure_monitoring_role", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def _no_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not connect to PostgreSQL")

    monkeypatch.setattr(module.psycopg, "connect", _no_connect)
    monkeypatch.setattr(module, "get_settings", _no_connect)
    monkeypatch.setenv("POSTGRES_MONITORING_USER", "monitoring")
    return module


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_password_skips_with_warning(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    monkeypatch.setenv("POSTGRES_MONITORING_PASSWORD", value)
    assert script.main() == 0
    err = capsys.readouterr().err
    assert "WARNING" in err and "skipped" in err and "openssl rand -hex 24" in err


@pytest.mark.parametrize("value", ["change_me", "CHANGE_ME", " change_me ", "changeme"])
def test_placeholder_password_is_config_error(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    monkeypatch.setenv("POSTGRES_MONITORING_PASSWORD", value)
    assert script.main() == 2
    err = capsys.readouterr().err
    assert "placeholder" in err
    assert value.strip() not in err  # the value itself is never printed


def test_scram_verifier_format(script: ModuleType) -> None:
    verifier = script.scram_sha256_verifier("abc", iterations=4096)
    assert verifier.startswith("SCRAM-SHA-256$4096:")
    assert "abc" not in verifier
