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
    "MASTER_BOT_PROXY_URL",
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
# Keys of .env.example that look like secrets but are deliberately not cleared by any role.
NOT_ROLE_SECRETS = {
    "MINIO_ACCESS_KEY",  # MinIO root login (not a password), app-only, default = project name
    "SENTRY_DSN",  # the only link between the hosts: VPS 2 writes it, VPS 1 needs it
}
SECRET_LIKE = re.compile(r"PASSWORD|SECRET|TOKEN|_KEY$|_KEYS|HASH|_PAT$|DSN|PROXY_URL")


def _secret_value(key: str) -> str:
    return f"secret-{key.lower()}-value"


def _write_env(tmp_path: Path, name: str = ".env") -> Path:
    lines = ["# header comment", ""]
    lines += [f"{key}={_secret_value(key)}" for key in APP_SECRETS + OBS_SECRETS]
    lines += [f"{k}={v}" for k, v in KEPT.items()]
    lines += ["# trailing comment", "export EXTRA_TOKEN=keep"]
    env = tmp_path / name
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env.chmod(0o640)
    return env


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed script path, arguments are tmp files
        ["sh", str(SCRIPT), *args],  # noqa: S607 - sh from PATH
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


def _ts_backups(env: Path) -> list[Path]:
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

    result = _run(role, str(env))

    assert result.returncode == 0, result.stderr
    values = _values(env)
    assert all(values[k] == "" for k in cleared)
    assert all(values[k] == old_values[k] for k in kept)
    assert all(values[k] == v for k, v in KEPT.items())
    assert values["EXTRA_TOKEN"] == "keep"
    text = env.read_text(encoding="utf-8")
    assert "# header comment" in text
    assert "# trailing comment" in text
    assert [ln.split("=", 1)[0] for ln in text.splitlines()] == [
        ln.split("=", 1)[0] for ln in before.splitlines()
    ]
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    backups = _ts_backups(env)
    assert len(backups) == 1
    assert re.fullmatch(r"\.env\.bak\.\d{14}(\.\d+)?", backups[0].name)
    assert backups[0].read_text(encoding="utf-8") == before
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert f"shred -u {backups[0]}" in result.stderr
    assert not list(tmp_path.glob(".*.role.*"))
    for k in cleared:
        assert old_values[k] not in result.stdout + result.stderr


def test_obs_role_clears_obs_bootstrap_backup(tmp_path: Path) -> None:
    """After ROLE=obs only the own .env.bak.<ts> still holds the app secrets."""
    env = _write_env(tmp_path)
    bak = _write_env(tmp_path, ".env.bak")  # cp -p .env .env.bak of obs_bootstrap.sh

    result = _run("obs", str(env))

    assert result.returncode == 0, result.stderr
    assert all(_values(bak)[k] == "" for k in APP_SECRETS)
    assert _values(bak)["URGENTRY_BOOTSTRAP_PAT"] == _secret_value("URGENTRY_BOOTSTRAP_PAT")
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600
    ts_backups = _ts_backups(env)
    assert len(ts_backups) == 1
    holders = {
        p.name
        for p in tmp_path.iterdir()
        if any(_secret_value(k) in p.read_text(encoding="utf-8") for k in APP_SECRETS)
    }
    assert holders == {ts_backups[0].name}


def test_obs_bootstrap_backup_cleared_even_if_env_is_clean(tmp_path: Path) -> None:
    env = _write_env(tmp_path)
    assert _run("obs", str(env)).returncode == 0
    bak = _write_env(tmp_path, ".env.bak")

    result = _run("obs", str(env))

    assert result.returncode == 0, result.stderr
    assert "unchanged" in result.stdout
    assert all(_values(bak)[k] == "" for k in APP_SECRETS)


def test_idempotent(tmp_path: Path) -> None:
    env = _write_env(tmp_path)
    assert _run("obs", str(env)).returncode == 0
    after_first = env.read_text(encoding="utf-8")

    second = _run("obs", str(env))

    assert second.returncode == 0, second.stderr
    assert "unchanged" in second.stdout
    assert env.read_text(encoding="utf-8") == after_first
    assert len(_ts_backups(env)) == 1


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("export MASTER_BOT_TOKEN=x", "export MASTER_BOT_TOKEN="),
        ('MASTER_BOT_TOKEN="x y"', "MASTER_BOT_TOKEN="),
        ("MASTER_BOT_TOKEN='x'", "MASTER_BOT_TOKEN="),
        ("MASTER_BOT_TOKEN= x", "MASTER_BOT_TOKEN="),
        ("  MASTER_BOT_TOKEN=x", "  MASTER_BOT_TOKEN="),
        ("MASTER_BOT_TOKEN=x\r", "MASTER_BOT_TOKEN="),  # CR of a cleared line is dropped
    ],
    ids=["export", "double-quoted", "single-quoted", "space-value", "indented", "crlf"],
)
def test_line_forms_are_cleared(tmp_path: Path, line: str, expected: str) -> None:
    env = tmp_path / ".env"
    env.write_bytes(f"A=1\r\n{line}\nB=2\n".encode())

    result = _run("obs", str(env))

    assert result.returncode == 0, result.stderr
    assert env.read_bytes() == f"A=1\r\n{expected}\nB=2\n".encode()


def test_duplicates_and_lines_without_equals(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("MASTER_BOT_TOKEN=a\njust text\nMASTER_BOT_TOKEN=b\n", encoding="utf-8")

    assert _run("obs", str(env)).returncode == 0

    assert env.read_text(encoding="utf-8") == "MASTER_BOT_TOKEN=\njust text\nMASTER_BOT_TOKEN=\n"


@pytest.mark.parametrize(
    "content", ["", "MASTER_BOT_TOKEN=\n", "# only comment\n"], ids=["empty", "cleared", "comment"]
)
def test_nothing_to_clear_keeps_file(tmp_path: Path, content: str) -> None:
    env = tmp_path / ".env"
    env.write_text(content, encoding="utf-8")

    result = _run("obs", str(env))

    assert result.returncode == 0, result.stderr
    assert "unchanged" in result.stdout
    assert env.read_text(encoding="utf-8") == content
    assert _ts_backups(env) == []


@pytest.mark.parametrize("name", [".env", ".env.bak"])
def test_symlink_is_refused(tmp_path: Path, name: str) -> None:
    real = _write_env(tmp_path, "real.env")
    before = real.read_text(encoding="utf-8")
    env = tmp_path / ".env"
    if name == ".env":
        env.symlink_to(real)
    else:
        _write_env(tmp_path)
        (tmp_path / ".env.bak").symlink_to(real)

    result = _run("obs", str(env))

    assert result.returncode != 0
    assert "symlink" in result.stderr
    assert real.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("role", ["", "both", "APP"])
def test_bad_role_is_rejected(tmp_path: Path, role: str) -> None:
    env = _write_env(tmp_path)
    before = env.read_text(encoding="utf-8")

    result = _run(role, str(env))

    assert result.returncode != 0
    assert "ROLE=app|obs" in result.stderr
    assert env.read_text(encoding="utf-8") == before
    assert _ts_backups(env) == []


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    result = _run("app", str(tmp_path / "absent.env"))

    assert result.returncode != 0
    assert "not found" in result.stderr


def test_list_matches_expected_roles() -> None:
    assert tuple(_run("--list", "app").stdout.split()) == APP_SECRETS
    assert tuple(_run("--list", "obs").stdout.split()) == OBS_SECRETS


def test_every_secret_of_env_example_has_a_role() -> None:
    """A new secret in .env.example must be added to APP_SECRETS/OBS_SECRETS of env_role.sh."""
    keys = set()
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and SECRET_LIKE.search(m.group(1)):
            keys.add(m.group(1))
    app = set(_run("--list", "app").stdout.split())
    obs = set(_run("--list", "obs").stdout.split())

    assert not app & obs
    assert keys - app - obs - NOT_ROLE_SECRETS == set()
    assert (app | obs) <= keys  # no stale names in the script


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
    assert _ts_backups(env) == []
