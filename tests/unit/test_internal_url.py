"""SENTRY_DSN / GLITCHTIP_INTERNAL_URL must point to an internal GlitchTip address."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import ObservabilitySettings, is_internal_host

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "scripts" / "check_internal_url.sh"

ALLOWED = [
    "http://k@glitchtip-web:8000/1",
    "http://k@10.10.0.2:8000/1",
    "http://k@172.20.0.5:8000/1",
    "http://k@192.168.1.10/1",
    "http://k@127.0.0.1:8000/1",
    "http://k@100.100.1.1:8000/1",
    "http://k@[::1]:8000/1",
    "http://k@localhost:8000/1",
]
FORBIDDEN = [
    "https://k@glitchtip.example.com/1",
    "http://k@8.8.8.8/1",
    "http://k@1.2.3.4.nip.io/1",
    "http://k@172.32.0.1/1",
    "http://k@100.128.0.1/1",
    "ftp://k@glitchtip-web/1",
    "http://k@/1",
    # numeric inet_aton forms: glibc getaddrinfo resolves them (134744072 -> 8.8.8.8)
    "http://k@134744072/1",
    "http://k@0x08080808/1",
    "http://k@0x8/1",
    "http://k@017700000001/1",
    "http://k@0x/1",
    "http://k@glitchtip-web:99999/1",
    "http://k@glitchtip-web:abc/1",
]


@pytest.fixture(autouse=True)
def _no_sentry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("SENTRY_DSN", "sentry_dsn"):
        monkeypatch.delenv(key, raising=False)


def _settings(dsn: str | None) -> ObservabilitySettings:
    return ObservabilitySettings(_env_file=None, sentry_dsn=dsn)  # type: ignore[call-arg,arg-type]


@pytest.mark.parametrize("dsn", ALLOWED)
def test_internal_dsn_is_accepted(dsn: str) -> None:
    settings = _settings(dsn)
    assert settings.sentry_dsn is not None
    assert settings.sentry_dsn.get_secret_value() == dsn


@pytest.mark.parametrize("dsn", FORBIDDEN)
def test_public_dsn_is_rejected(dsn: str) -> None:
    with pytest.raises(ValidationError, match="SENTRY_DSN must point to an internal") as exc:
        _settings(dsn)
    assert dsn not in str(exc.value)  # the DSN (with its key) is never echoed


@pytest.mark.parametrize("dsn", [None, "", "   "])
def test_empty_dsn_disables_sentry(dsn: str | None) -> None:
    assert _settings(dsn).sentry_dsn is None


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("GLITCHTIP-WEB", True),
        ("obs-postgres", True),
        ("fd00::1", True),
        ("2001:db8::1", False),
        ("169.254.1.1", False),
        ("134744072", False),
        ("0x08080808", False),
        ("0xa", False),
        ("012", False),
    ],
)
def test_is_internal_host(host: str, expected: bool) -> None:
    assert is_internal_host(host) is expected


@pytest.mark.skipif(shutil.which("python3") is None, reason="python3 is required")
@pytest.mark.parametrize(
    ("url", "ok"), [(u, True) for u in ALLOWED] + [(u, False) for u in FORBIDDEN]
)
def test_shell_check_matches_validator(url: str, ok: bool) -> None:
    result = subprocess.run(  # noqa: S603 - fixed script path
        ["bash", str(SCRIPT), url],  # noqa: S607 - bash from PATH
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode == 0) is ok, result.stderr
    if not ok:
        assert "is not internal" in result.stderr
        assert url not in result.stderr
