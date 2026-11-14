"""``ops/scripts/obs_bootstrap.sh``: urgentry DSN via API, env precedence, secrets masking.

``make obs-up`` runs ``obs_bootstrap.sh --prepare`` before ``docker compose up`` (urgentry creates
the owner/PAT from ``URGENTRY_BOOTSTRAP_*`` on the first start), then ``make obs-bootstrap``
reads the default project key via the API and writes ``SENTRY_DSN``. The script runs after
``set -a; source .env``, so generated values must also be exported to the process environment.
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

# Fake docker: `compose ... exec ...` dumps its environment to $FAKE_DOCKER_ENV_OUT and answers
# like urgentry (healthz / projects / keys by the last argument), or (FAKE_DOCKER_FAIL=1) fails
# the projects request.
_FAKE_DOCKER = f"""#!/bin/sh
case " $* " in *" exec "*) ;; *) exit 0;; esac
env >"$FAKE_DOCKER_ENV_OUT"
for last in "$@"; do :; done
pid=${{FAKE_PROJECT_ID:-default-project}}
case "$last" in
    */healthz) echo '{{"status":"ok"}}';;
    /api/0/projects/)
        [ "${{FAKE_DOCKER_FAIL:-0}}" = 1 ] && {{ echo "401 Unauthorized" >&2; exit 1; }}
        printf '[{{"id":"%s","slug":"default","organization":"urgentry-org"}}]\\n' "$pid";;
    /api/0/projects/urgentry-org/default/keys/)
        dsn="http://{PUBLIC_KEY_HEX}@u:8080/$pid"
        printf '[{{"public":"{PUBLIC_KEY_HEX}","dsn":{{"public":"%s"}}}}]\\n' "$dsn";;
    *) echo "unexpected $last" >&2; exit 1;;
esac
"""


def _values(text: str) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def _run(
    tmp_path: Path,
    env_text: str,
    *,
    fail: bool = False,
    prepare: bool = False,
    project_id: str = "default-project",
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    env_file = tmp_path / "x.env"
    env_file.write_text(env_text, encoding="utf-8")
    env_out = tmp_path / "docker.env"
    args = ["--prepare"] if prepare else []
    result = subprocess.run(  # noqa: S603 - fixed script path, arguments are tmp files
        [shutil.which("bash") or "bash", str(OBS_BOOTSTRAP), *args, str(env_file)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=60,
        env={
            "PATH": f"{bindir}:/usr/local/bin:/usr/bin:/bin",
            # as after `set -a; source .env` in make obs-bootstrap
            "URGENTRY_BOOTSTRAP_PASSWORD": "",
            "URGENTRY_BOOTSTRAP_PAT": "",
            "URGENTRY_INTERNAL_URL": "",
            "FAKE_DOCKER_ENV_OUT": str(env_out),
            "FAKE_DOCKER_FAIL": "1" if fail else "0",
            "FAKE_PROJECT_ID": project_id,
        },
    )
    return result, env_file, env_out


_EMPTY_ENV = "PROJECT_ENVIRONMENT=loc\nURGENTRY_BOOTSTRAP_PASSWORD=\nURGENTRY_BOOTSTRAP_PAT=\n"


def test_generated_values_reach_docker_despite_empty_shell_env(tmp_path: Path) -> None:
    result, env_file, env_out = _run(tmp_path, _EMPTY_ENV)

    assert result.returncode == 0, result.stderr
    written = _values(env_file.read_text(encoding="utf-8"))
    seen = _values(env_out.read_text(encoding="utf-8"))
    for key in (
        "URGENTRY_BOOTSTRAP_PASSWORD",
        "URGENTRY_BOOTSTRAP_PAT",
        "URGENTRY_INTERNAL_URL",
        "URGENTRY_BOOTSTRAP_EMAIL",
    ):
        assert written[key], key
        assert seen[key] == written[key], key
    assert len(written["URGENTRY_BOOTSTRAP_PAT"]) == 48
    assert written["SENTRY_DSN"] == f"http://{PUBLIC_KEY_HEX}@caddy:8081/1"
    assert written["URGENTRY_INTERNAL_URL"] == "http://caddy:8081"
    assert "urgentry bootstrap OK" in result.stdout
    output = result.stdout + result.stderr
    assert written["URGENTRY_BOOTSTRAP_PASSWORD"] not in output
    assert written["URGENTRY_BOOTSTRAP_PAT"] not in output


def test_prepare_only_generates_secrets(tmp_path: Path) -> None:
    result, env_file, env_out = _run(tmp_path, _EMPTY_ENV, prepare=True)

    assert result.returncode == 0, result.stderr
    written = _values(env_file.read_text(encoding="utf-8"))
    assert written["URGENTRY_BOOTSTRAP_PAT"]
    assert "SENTRY_DSN" not in written
    assert not env_out.exists()


def test_rerun_reports_dsn_up_to_date(tmp_path: Path) -> None:
    first, env_file, _ = _run(tmp_path, _EMPTY_ENV)
    assert first.returncode == 0, first.stderr
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second, _, _ = _run(second_dir, env_file.read_text(encoding="utf-8"))

    assert second.returncode == 0, second.stderr
    assert "SENTRY_DSN is up to date" in second.stdout
    assert "Updated" not in second.stdout


def test_test_environment_does_not_write_dsn(tmp_path: Path) -> None:
    result, env_file, _ = _run(tmp_path, _EMPTY_ENV.replace("=loc", "=test"))

    assert result.returncode == 0, result.stderr
    assert "SENTRY_DSN" not in _values(env_file.read_text(encoding="utf-8"))


def test_api_failure_does_not_write_dsn_or_secrets(tmp_path: Path) -> None:
    result, env_file, _ = _run(tmp_path, _EMPTY_ENV, fail=True)

    assert result.returncode == 1
    values = _values(env_file.read_text(encoding="utf-8"))
    assert "ERROR: urgentry API /api/0/projects/ failed" in result.stderr
    assert values["URGENTRY_BOOTSTRAP_PAT"] not in result.stdout + result.stderr
    assert "SENTRY_DSN" not in values


def test_non_hex_secret_is_rejected(tmp_path: Path) -> None:
    result, env_file, _ = _run(tmp_path, "URGENTRY_BOOTSTRAP_PASSWORD=change_me\n")

    assert result.returncode == 1
    assert "URGENTRY_BOOTSTRAP_PASSWORD" in result.stderr
    assert "SENTRY_DSN" not in _values(env_file.read_text(encoding="utf-8"))


def test_old_direct_url_and_dsn_are_migrated(tmp_path: Path) -> None:
    old = (
        _EMPTY_ENV
        + "URGENTRY_INTERNAL_URL=http://urgentry:8080\n"
        + f"SENTRY_DSN=http://{PUBLIC_KEY_HEX}@urgentry:8080/default-project\n"
    )
    result, env_file, _ = _run(tmp_path, old)

    assert result.returncode == 0, result.stderr
    written = _values(env_file.read_text(encoding="utf-8"))
    assert written["URGENTRY_INTERNAL_URL"] == "http://caddy:8081"
    assert written["SENTRY_DSN"] == f"http://{PUBLIC_KEY_HEX}@caddy:8081/1"
    assert "Updated SENTRY_DSN" in result.stdout


def test_unexpected_project_id_is_rejected(tmp_path: Path) -> None:
    result, env_file, _ = _run(tmp_path, _EMPTY_ENV, project_id="0a1b2c3d")

    assert result.returncode == 1
    assert "Caddy :8081 maps /api/1/ to default-project" in result.stderr
    assert "SENTRY_DSN" not in _values(env_file.read_text(encoding="utf-8"))
