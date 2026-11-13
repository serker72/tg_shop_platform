"""``ops/scripts/obs_bootstrap.sh``: env precedence over ``--env-file`` and error masking.

``make obs-bootstrap`` runs the script after ``set -a; source .env``, so empty
``GLITCHTIP_ADMIN_PASSWORD=`` / ``GLITCHTIP_PROJECT_PUBLIC_KEY=`` are already exported; the
shell environment wins over ``docker compose --env-file``. Generated values must therefore
reach ``docker compose run`` through the process environment as well.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OBS_BOOTSTRAP = ROOT / "ops" / "scripts" / "obs_bootstrap.sh"
PUBLIC_KEY_HEX = "0123456789abcdef0123456789abcdef"

pytestmark = [
    pytest.mark.req("SEC1"),
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required"),
    pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is required"),
    pytest.mark.skipif(shutil.which("python3") is None, reason="python3 is required"),
]

# Fake docker: `compose ... run ...` dumps its environment to $FAKE_DOCKER_ENV_OUT and prints
# the bootstrap marker, or (FAKE_DOCKER_FAIL=1) a diagnostic + a line with the password.
_FAKE_DOCKER = f"""#!/bin/sh
for arg in "$@"; do
    if [ "$arg" = run ]; then
        env >"$FAKE_DOCKER_ENV_OUT"
        if [ "${{FAKE_DOCKER_FAIL:-0}}" = 1 ]; then
            echo "GLITCHTIP_BOOTSTRAP_ERROR GLITCHTIP_ADMIN_PASSWORD is empty" >&2
            echo "leak: $GLITCHTIP_ADMIN_PASSWORD"
            exit 1
        fi
        echo "glitchtip-bootstrap: superuser exists"
        echo "GLITCHTIP_BOOTSTRAP_OK project_id=1 public_key={PUBLIC_KEY_HEX}"
        exit 0
    fi
done
exit 0
"""


def _values(text: str) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def _run(
    tmp_path: Path, env_text: str, *, fail: bool = False
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    env_file = tmp_path / "x.env"
    env_file.write_text(env_text, encoding="utf-8")
    env_out = tmp_path / "docker.env"
    result = subprocess.run(  # noqa: S603 - fixed script path, arguments are tmp files
        [shutil.which("bash") or "bash", str(OBS_BOOTSTRAP), str(env_file)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=60,
        env={
            "PATH": f"{bindir}:/usr/local/bin:/usr/bin:/bin",
            # as after `set -a; source .env` in make obs-bootstrap
            "GLITCHTIP_ADMIN_PASSWORD": "",
            "GLITCHTIP_PROJECT_PUBLIC_KEY": "",
            "GLITCHTIP_INTERNAL_URL": "",
            "FAKE_DOCKER_ENV_OUT": str(env_out),
            "FAKE_DOCKER_FAIL": "1" if fail else "0",
        },
    )
    return result, env_file, env_out


_EMPTY_ENV = "PROJECT_ENVIRONMENT=loc\nGLITCHTIP_ADMIN_PASSWORD=\nGLITCHTIP_PROJECT_PUBLIC_KEY=\n"


def test_generated_values_reach_docker_despite_empty_shell_env(tmp_path: Path) -> None:
    result, env_file, env_out = _run(tmp_path, _EMPTY_ENV)

    assert result.returncode == 0, result.stderr
    written = _values(env_file.read_text(encoding="utf-8"))
    seen = _values(env_out.read_text(encoding="utf-8"))
    for key in (
        "GLITCHTIP_ADMIN_PASSWORD",
        "GLITCHTIP_PROJECT_PUBLIC_KEY",
        "GLITCHTIP_INTERNAL_URL",
        "GLITCHTIP_ORG_SLUG",
    ):
        assert written[key], key
        assert seen[key] == written[key], key
    assert written["SENTRY_DSN"] == f"http://{PUBLIC_KEY_HEX}@glitchtip-web:8000/1"
    assert "GlitchTip bootstrap OK" in result.stdout
    assert written["GLITCHTIP_ADMIN_PASSWORD"] not in result.stdout + result.stderr


def test_rerun_reports_dsn_up_to_date(tmp_path: Path) -> None:
    first, env_file, _ = _run(tmp_path, _EMPTY_ENV)
    assert first.returncode == 0, first.stderr
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second, _, _ = _run(second_dir, env_file.read_text(encoding="utf-8"))

    assert second.returncode == 0, second.stderr
    assert "SENTRY_DSN is up to date" in second.stdout
    assert "Updated" not in second.stdout


def test_failure_shows_diagnostics_but_not_password(tmp_path: Path) -> None:
    result, env_file, _ = _run(tmp_path, _EMPTY_ENV, fail=True)

    assert result.returncode == 1
    password = _values(env_file.read_text(encoding="utf-8"))["GLITCHTIP_ADMIN_PASSWORD"]
    assert password
    assert "GLITCHTIP_BOOTSTRAP_ERROR GLITCHTIP_ADMIN_PASSWORD is empty" in result.stderr
    assert "ERROR: glitchtip-bootstrap failed" in result.stderr
    assert password not in result.stdout + result.stderr
    assert "SENTRY_DSN" not in _values(env_file.read_text(encoding="utf-8"))
