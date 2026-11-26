"""``ops/scripts/obs_bootstrap.sh``: urgentry DSN via API, env precedence, secrets masking.

``make obs-up`` runs ``obs_bootstrap.sh --prepare`` before ``docker compose up`` (urgentry creates
the owner/PAT from ``URGENTRY_BOOTSTRAP_*`` on the first start), then ``make obs-bootstrap``
reads the default project key via the API and writes ``SENTRY_DSN``. The script runs after
``set -a; source .env``, so generated values must also be exported to the process environment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

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
    /api/0/projects/urgentry-org/default/settings/)
        if [ -n "${{FAKE_SETTINGS:-}}" ]; then printf '%s\\n' "$FAKE_SETTINGS"; exit 0; fi
        printf '{{"name":"Default","status":"active","eventRetentionDays":%s}}\\n' \
            "${{FAKE_RETENTION_DAYS:-30}}";;
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
    extra_env: dict[str, str] | None = None,
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
            **(extra_env or {}),
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
    assert set(tmp_path.iterdir()) == {env_file, tmp_path / "fakebin"}


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


@pytest.mark.parametrize(
    ("url", "warned"),
    [
        ("http://caddy:8081", False),
        ("http://10.10.0.2:8081", False),
        ("http://10.10.0.2:8000", True),
    ],
)
def test_internal_url_must_be_caddy_ingest(tmp_path: Path, url: str, warned: bool) -> None:
    result, env_file, _ = _run(tmp_path, _EMPTY_ENV + f"URGENTRY_INTERNAL_URL={url}\n")

    assert result.returncode == 0, result.stderr
    assert ("WARNING: URGENTRY_INTERNAL_URL" in result.stderr) is warned
    host = url.removeprefix("http://")
    assert _values(env_file.read_text(encoding="utf-8"))["SENTRY_DSN"] == (
        f"http://{PUBLIC_KEY_HEX}@{host}/1"
    )


# Event retention: GET settings goes through the fake docker (wget in the urgentry container),
# the PUT goes from host python3 to URGENTRY_API_URL -> a local HTTP server records it.
class _SettingsHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, str, dict[str, object]]]] = []
    status: ClassVar[int] = 200
    answer_days: ClassVar[int | None] = None

    def do_PUT(self) -> None:  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, self.headers["Authorization"], body))
        cls = type(self)
        if cls.status != 200:
            self.send_response(cls.status)
            self.end_headers()
            self.wfile.write(b'{"detail":"error"}')
            return
        days = body["eventRetentionDays"] if cls.answer_days is None else cls.answer_days
        answer = json.dumps({"eventRetentionDays": days}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args: object) -> None:
        pass


def _argv_logging_python(tmp_path: Path) -> tuple[Path, Path]:
    """Wrapper dir with python3 that appends its argv to a log, then runs the real python3."""
    wrapdir = tmp_path / "wrapbin"
    wrapdir.mkdir()
    log = tmp_path / "python3.argv"
    real = shutil.which("python3") or "python3"
    wrapper = wrapdir / "python3"
    wrapper.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >>"{log}"\nexec "{real}" "$@"\n')
    wrapper.chmod(0o755)
    return wrapdir, log


@pytest.mark.parametrize(("env_days", "expected"), [("", 30), ("14", 14)])
def test_event_retention_is_set_via_settings_api(
    tmp_path: Path, env_days: str, expected: int
) -> None:
    _SettingsHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _SettingsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    wrapdir, argv_log = _argv_logging_python(tmp_path)
    env_text = _EMPTY_ENV + (f"URGENTRY_EVENT_RETENTION_DAYS={env_days}\n" if env_days else "")
    try:
        result, env_file, _ = _run(
            tmp_path,
            env_text,
            extra_env={
                "FAKE_RETENTION_DAYS": "90",
                "URGENTRY_API_URL": f"http://127.0.0.1:{server.server_port}",
                "PATH": f"{wrapdir}:{tmp_path / 'fakebin'}:/usr/local/bin:/usr/bin:/bin",
            },
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stderr
    assert f"eventRetentionDays: 90 -> {expected}" in result.stdout
    [(path, auth, body)] = _SettingsHandler.requests
    assert path == "/api/0/projects/urgentry-org/default/settings/"
    pat = _values(env_file.read_text(encoding="utf-8"))["URGENTRY_BOOTSTRAP_PAT"]
    assert pat
    assert auth == f"Bearer {pat}"
    # current settings are kept, only the retention changes; policies are derived by urgentry
    assert body == {"name": "Default", "status": "active", "eventRetentionDays": expected}
    # the PAT reaches python3 via the environment only: never in argv or in the output
    assert argv_log.read_text(encoding="utf-8")
    assert pat not in argv_log.read_text(encoding="utf-8")
    assert pat not in result.stdout + result.stderr


def test_event_retention_default_30_up_to_date(tmp_path: Path) -> None:
    # FAKE_RETENTION_DAYS defaults to 30 and no API server is listening on URGENTRY_API_URL:
    # a PUT attempt would fail the script.
    result, _, _ = _run(
        tmp_path,
        _EMPTY_ENV,
        extra_env={"URGENTRY_API_URL": "http://127.0.0.1:9"},
    )

    assert result.returncode == 0, result.stderr
    assert "urgentry eventRetentionDays=30 is up to date" in result.stdout
    assert "->" not in result.stdout


def _run_retention(
    tmp_path: Path,
    *,
    env_extra: str = "",
    settings: str | None = None,
    status: int = 200,
    answer_days: int | None = None,
    api_url: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], str]:
    _SettingsHandler.requests = []
    _SettingsHandler.status = status
    _SettingsHandler.answer_days = answer_days
    server = HTTPServer(("127.0.0.1", 0), _SettingsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    wrapdir, argv_log = _argv_logging_python(tmp_path)
    extra = {
        "FAKE_RETENTION_DAYS": "90",
        "URGENTRY_API_URL": api_url or f"http://127.0.0.1:{server.server_port}",
        "PATH": f"{wrapdir}:{tmp_path / 'fakebin'}:/usr/local/bin:/usr/bin:/bin",
    }
    if settings is not None:
        extra["FAKE_SETTINGS"] = settings
    try:
        result, env_file, _ = _run(tmp_path, _EMPTY_ENV + env_extra, extra_env=extra)
    finally:
        server.shutdown()
        server.server_close()
        _SettingsHandler.status = 200
        _SettingsHandler.answer_days = None
    values = _values(env_file.read_text(encoding="utf-8"))
    argv = argv_log.read_text(encoding="utf-8") if argv_log.exists() else ""
    return result, values, argv


def _assert_retention_skipped(
    result: subprocess.CompletedProcess[str], values: dict[str, str], argv: str
) -> None:
    assert result.returncode == 0, result.stderr
    assert "WARNING: urgentry event retention not applied:" in result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert values["SENTRY_DSN"] == f"http://{PUBLIC_KEY_HEX}@caddy:8081/1"
    pat = values["URGENTRY_BOOTSTRAP_PAT"]
    assert pat
    assert pat not in argv
    assert pat not in result.stdout + result.stderr


@pytest.mark.parametrize("status", [400, 500])
def test_event_retention_put_http_error_is_a_warning(tmp_path: Path, status: int) -> None:
    result, values, argv = _run_retention(tmp_path, status=status)

    _assert_retention_skipped(result, values, argv)
    assert f"HTTP {status}" in result.stderr
    assert len(_SettingsHandler.requests) == 1


def test_event_retention_get_not_json_is_a_warning(tmp_path: Path) -> None:
    result, values, argv = _run_retention(tmp_path, settings="<html>502 Bad Gateway</html>")

    _assert_retention_skipped(result, values, argv)
    assert _SettingsHandler.requests == []


def test_event_retention_put_answer_mismatch_is_a_warning(tmp_path: Path) -> None:
    result, values, argv = _run_retention(tmp_path, answer_days=7)

    _assert_retention_skipped(result, values, argv)
    assert "eventRetentionDays=7, expected 30" in result.stderr


@pytest.mark.parametrize("days", ["0", "abc", "030"])
def test_event_retention_invalid_value_is_a_warning(tmp_path: Path, days: str) -> None:
    result, values, argv = _run_retention(
        tmp_path, env_extra=f"URGENTRY_EVENT_RETENTION_DAYS={days}\n"
    )

    _assert_retention_skipped(result, values, argv)
    assert _SettingsHandler.requests == []


def test_event_retention_public_api_url_is_skipped_without_pat(tmp_path: Path) -> None:
    result, values, argv = _run_retention(tmp_path, api_url="http://8.8.8.8:8000")

    _assert_retention_skipped(result, values, argv)
    assert "not internal" in result.stderr
    assert _SettingsHandler.requests == []


def test_event_retention_urgentry_port_unreachable_is_a_warning(tmp_path: Path) -> None:
    # urgentry API port is not reachable from the host (nothing listens on port 9)
    result, values, argv = _run_retention(tmp_path, api_url="http://127.0.0.1:9")

    _assert_retention_skipped(result, values, argv)
    assert "PUT settings/ failed" in result.stderr


def test_event_retention_keeps_telemetry_policies(tmp_path: Path) -> None:
    policies = [
        {"surface": "errors", "retentionDays": 90, "storageTier": "delete"},
        {"surface": "logs", "retentionDays": 14, "storageTier": "delete"},  # manual override
        {"surface": "attachments", "retentionDays": 30, "storageTier": "delete"},
    ]
    settings = json.dumps(
        {"name": "Default", "eventRetentionDays": 90, "telemetryPolicies": policies}
    )

    result, values, _ = _run_retention(tmp_path, settings=settings)

    assert result.returncode == 0, result.stderr
    assert "WARNING" not in result.stderr
    assert values["SENTRY_DSN"] == f"http://{PUBLIC_KEY_HEX}@caddy:8081/1"
    [(_, _, body)] = _SettingsHandler.requests
    assert body["eventRetentionDays"] == 30
    assert body["telemetryPolicies"] == [
        {"surface": "errors", "retentionDays": 30, "storageTier": "delete"},
        {"surface": "logs", "retentionDays": 14, "storageTier": "delete"},
        {"surface": "attachments", "retentionDays": 30, "storageTier": "delete"},
    ]
