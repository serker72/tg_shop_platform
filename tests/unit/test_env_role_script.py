"""``ops/scripts/env_role.sh``: one .env for VPS 1 + VPS 2, secrets of the other host cleared."""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "scripts" / "env_role.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="sh is required")

APP_SECRETS = (
    "POSTGRES_PASSWORD",
    "POSTGRES_MONITORING_PASSWORD",
    "REDIS_PASSWORD",
    "RABBITMQ_PASSWORD",
    "MINIO_SECRET_KEY",
    "STORAGE_ACCESS_KEY",
    "STORAGE_SECRET_KEY",
    "ENCRYPTION_KEY",
    "ENCRYPTION_KEYS_OLD",
    "MASTER_BOT_TOKEN",
)
OBS_SECRETS = (
    "GRAFANA_ADMIN_PASSWORD",
    "CROWDSEC_BOUNCER_KEY",
    "OBS_AUTH_HASH",
    "URGENTRY_BOOTSTRAP_PASSWORD",
    "URGENTRY_BOOTSTRAP_PAT",
    "ALERT_TELEGRAM_BOT_TOKEN",
)
KEPT = {
    "PROJECT_NAME": "tg_shop_platform",
    "SENTRY_DSN": "http://abc@10.10.0.2:8081/1",
    "MINIO_ACCESS_KEY": "tg_shop_platform",
    "OBS_AUTH_USER": "admin",
    "ALERT_TELEGRAM_CHAT_ID": "-100123",
    "LOGS_DATA_DIR": '"${PROJECT_DATA_DIR}/logs"',
}


def _write_env(tmp_path: Path) -> Path:
    lines = ["# header comment", ""]
    for i, key in enumerate(APP_SECRETS + OBS_SECRETS):
        lines.append(f"{key}=secret{i:02d}value")
    lines += [f"{k}={v}" for k, v in KEPT.items()]
    lines += ["# trailing comment", "export EXTRA_TOKEN=keep"]
    env = tmp_path / ".env"
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env.chmod(0o640)
    return env


def _run(role: str, env: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed script path, arguments are tmp files
        ["sh", str(SCRIPT), role, str(env)],  # noqa: S607 - sh from PATH
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=60,
    )


def _values(env: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in env.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.removeprefix("export ").split("=", 1)
            result[key] = value
    return result


def _backups(env: Path) -> list[Path]:
    return sorted(env.parent.glob(f"{env.name}.bak.*"))


@pytest.mark.parametrize(
    ("role", "cleared", "kept"),
    [("app", OBS_SECRETS, APP_SECRETS), ("obs", APP_SECRETS, OBS_SECRETS)],
)
def test_role_clears_other_host_secrets(
    tmp_path: Path, role: str, cleared: tuple[str, ...], kept: tuple[str, ...]
) -> None:
    env = _write_env(tmp_path)
    before = env.read_text(encoding="utf-8")
    old_values = _values(env)

    result = _run(role, env)

    assert result.returncode == 0, result.stderr
    values = _values(env)
    assert all(values[k] == "" for k in cleared)
    assert all(values[k] == old_values[k] for k in kept)
    assert all(values[k] == v for k, v in KEPT.items())
    assert values["EXTRA_TOKEN"] == "keep"
    text = env.read_text(encoding="utf-8")
    assert "# header comment" in text
    assert "# trailing comment" in text
    # order and line count are preserved, only values change
    assert [ln.split("=", 1)[0] for ln in text.splitlines()] == [
        ln.split("=", 1)[0] for ln in before.splitlines()
    ]
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    backups = _backups(env)
    assert len(backups) == 1
    assert re.fullmatch(r"\.env\.bak\.\d{14}(\.\d+)?", backups[0].name)
    assert backups[0].read_text(encoding="utf-8") == before
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.role.*"))
    for k in cleared:
        assert old_values[k] not in result.stdout + result.stderr


def test_idempotent(tmp_path: Path) -> None:
    env = _write_env(tmp_path)
    assert _run("obs", env).returncode == 0
    after_first = env.read_text(encoding="utf-8")

    second = _run("obs", env)

    assert second.returncode == 0, second.stderr
    assert "unchanged" in second.stdout
    assert env.read_text(encoding="utf-8") == after_first
    assert len(_backups(env)) == 1


@pytest.mark.parametrize("role", ["", "both", "APP"])
def test_bad_role_is_rejected(tmp_path: Path, role: str) -> None:
    env = _write_env(tmp_path)
    before = env.read_text(encoding="utf-8")

    result = _run(role, env)

    assert result.returncode != 0
    assert "ROLE=app|obs" in result.stderr
    assert env.read_text(encoding="utf-8") == before
    assert _backups(env) == []


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    result = _run("app", tmp_path / "absent.env")

    assert result.returncode != 0
    assert "not found" in result.stderr


@pytest.mark.skipif(shutil.which("make") is None, reason="make is required")
def test_make_env_role_requires_role(tmp_path: Path) -> None:
    env = _write_env(tmp_path)
    result = subprocess.run(  # noqa: S603
        ["make", "--no-print-directory", "env-role", f"ENV_FILE={env}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=60,
    )
    assert result.returncode != 0
    assert "ROLE=app|obs" in result.stderr
    assert _backups(env) == []
