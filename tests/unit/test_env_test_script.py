"""``ops/scripts/env_test.sh``: the test env must point at a ``*_test`` database."""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "scripts" / "env_test.sh"

pytestmark = [
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required"),
    pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is required"),
]


def _write_source(tmp_path: Path, *, postgres_db: str | None) -> Path:
    lines = [
        "PROJECT_ENVIRONMENT=loc",
        'PROJECT_NAME="shop"',
        f'PROJECT_DATA_DIR="{tmp_path}/data/shop"',
        "POSTGRES_PORT_HOST=5432",
        "POSTGRES_PASSWORD=dummy-password",
        f"ENCRYPTION_KEY={Fernet.generate_key().decode()}",
    ]
    if postgres_db is not None:
        lines.append(f"POSTGRES_DB={postgres_db}")
    source = tmp_path / "src.env"
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return source


def _run(source: Path, target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed script path, arguments are tmp files
        ["bash", str(SCRIPT), str(source), str(target)],  # noqa: S607 - bash from PATH
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=120,
    )


def _values(target: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key] = value.strip('"')
    return result


def test_postgres_db_gets_test_suffix(tmp_path: Path) -> None:
    target = tmp_path / "dst.env"
    result = _run(_write_source(tmp_path, postgres_db='"shop_db"'), target)

    assert result.returncode == 0, result.stderr
    values = _values(target)
    assert values["POSTGRES_DB"] == "shop_db_test"
    assert values["PROJECT_ENVIRONMENT"] == "test"
    assert values["PROJECT_NAME"] == "shop_test"
    assert values["POSTGRES_PORT_HOST"] == "25432"


@pytest.mark.parametrize("raw", ["  shop_db  ", '"  shop_db  "'], ids=["bare", "quoted"])
def test_postgres_db_surrounding_spaces_are_trimmed(tmp_path: Path, raw: str) -> None:
    target = tmp_path / "dst.env"
    result = _run(_write_source(tmp_path, postgres_db=raw), target)

    assert result.returncode == 0, result.stderr
    assert 'POSTGRES_DB="shop_db_test"' in target.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("postgres_db", "reason"),
    [
        ("shop_test", "POSTGRES_DB already ends with _test"),
        (None, "POSTGRES_DB is empty"),
        ("   ", "POSTGRES_DB is empty"),
        ('"   "', "POSTGRES_DB is empty"),
        ("' '", "POSTGRES_DB is empty"),
    ],
    ids=["test-suffix", "missing", "blank", "blank-double-quoted", "blank-single-quoted"],
)
def test_bad_postgres_db_is_rejected(tmp_path: Path, postgres_db: str | None, reason: str) -> None:
    target = tmp_path / "dst.env"
    result = _run(_write_source(tmp_path, postgres_db=postgres_db), target)

    assert result.returncode != 0
    assert reason in result.stderr
    assert not target.exists()


# --- --init mode (make env) ---

HEX48 = re.compile(r"^[0-9a-f]{48}$")


def _run_init(source: Path, target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed script path, arguments are tmp files
        ["bash", str(SCRIPT), "--init", str(source), str(target)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=120,
    )


def _write_template(tmp_path: Path) -> Path:
    source = tmp_path / "example.env"
    source.write_text(
        "\n".join(
            [
                "# comment stays",
                "PROJECT_ENVIRONMENT=stage",
                "PROJECT_DATA_DIR=/opt/tg_shop_platform_data",
                'LOGS_DATA_DIR="${PROJECT_DATA_DIR}/logs"',
                "POSTGRES_PASSWORD=change_me",
                "REDIS_PASSWORD=change_me",
                "MINIO_SECRET_KEY=change_me_min_8_chars",
                "ENCRYPTION_KEY=",
                "POSTGRES_MONITORING_PASSWORD=",
                "MASTER_BOT_TOKEN=",
                "URGENTRY_BOOTSTRAP_PASSWORD=",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return source


def test_init_generates_secrets_and_copies_the_rest(tmp_path: Path) -> None:
    target = tmp_path / "out.env"
    result = _run_init(_write_template(tmp_path), target)

    assert result.returncode == 0, result.stderr
    values = _values(target)
    secrets = [
        values[k]
        for k in (
            "POSTGRES_PASSWORD",
            "REDIS_PASSWORD",
            "MINIO_SECRET_KEY",
            "POSTGRES_MONITORING_PASSWORD",
        )
    ]
    assert all(HEX48.match(s) for s in secrets), secrets
    assert len(set(secrets)) == len(secrets)
    Fernet(values["ENCRYPTION_KEY"].encode())
    assert values["PROJECT_ENVIRONMENT"] == "stage"
    assert values["PROJECT_DATA_DIR"] == "/opt/tg_shop_platform_data"
    assert values["LOGS_DATA_DIR"] == "${PROJECT_DATA_DIR}/logs"
    assert values["MASTER_BOT_TOKEN"] == ""
    assert values["URGENTRY_BOOTSTRAP_PASSWORD"] == ""
    assert "# comment stays" in target.read_text(encoding="utf-8")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "POSTGRES_PASSWORD" in result.stdout
    assert "MASTER_BOT_TOKEN" in result.stdout
    assert secrets[0] not in result.stdout


def test_init_refuses_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "out.env"
    target.write_text("KEEP=1\n", encoding="utf-8")
    result = _run_init(_write_template(tmp_path), target)

    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert target.read_text(encoding="utf-8") == "KEEP=1\n"


def test_init_does_not_change_default_mode(tmp_path: Path) -> None:
    """Legacy ``env_test.sh <src> <dst>`` still applies the test transformations."""
    target = tmp_path / "dst.env"
    result = _run(_write_source(tmp_path, postgres_db="shop_db"), target)

    assert result.returncode == 0, result.stderr
    assert _values(target)["PROJECT_ENVIRONMENT"] == "test"


def test_init_fails_without_openssl(tmp_path: Path) -> None:
    """No openssl -> non-zero exit, no target, no temp file left behind."""
    out = tmp_path / "out"
    out.mkdir()
    target = out / "x.env"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("realpath", "mktemp", "dirname", "basename", "chmod", "rm", "ln", "tr"):
        found = shutil.which(tool)
        assert found is not None
        (bindir / tool).symlink_to(found)
    bash = shutil.which("bash") or "bash"
    source = _write_template(tmp_path)
    result = subprocess.run(  # noqa: S603 - fixed script path, arguments are tmp files
        [bash, str(SCRIPT), "--init", str(source), str(target)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=120,
        env={"PATH": str(bindir)},
    )

    assert result.returncode != 0
    assert not target.exists()
    assert list(out.iterdir()) == []


@pytest.mark.skipif(shutil.which("make") is None, reason="make is required")
def test_make_env_skip_and_force(tmp_path: Path) -> None:
    target = tmp_path / "x.env"

    def make(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["make", "--no-print-directory", "env", f"ENV_FILE={target}", *extra],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT,
            timeout=120,
        )

    first = make()
    assert first.returncode == 0, first.stderr
    old = target.read_text(encoding="utf-8")
    old_pw = _values(target)["POSTGRES_PASSWORD"]

    skip = make()
    assert skip.returncode == 0
    assert "SKIP" in skip.stdout
    assert target.read_text(encoding="utf-8") == old

    forced = make("FORCE=1")
    assert forced.returncode == 0, forced.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert _values(target)["POSTGRES_PASSWORD"] != old_pw
    assert list(tmp_path.iterdir()) == [target]
    for proc in (first, skip, forced):
        assert old_pw not in proc.stdout + proc.stderr


def test_init_real_env_example(tmp_path: Path) -> None:
    target = tmp_path / "real.env"
    result = _run_init(ROOT / ".env.example", target)

    assert result.returncode == 0, result.stderr
    text = target.read_text(encoding="utf-8")
    values = _values(target)
    assert "change_me" not in "\n".join(values.values())
    Fernet(values["ENCRYPTION_KEY"].encode())
    assert HEX48.match(values["POSTGRES_MONITORING_PASSWORD"])
    assert text.count("\n") == (ROOT / ".env.example").read_text(encoding="utf-8").count("\n")


_TEST_PG_PROFILE = {
    "PG_MEM_LIMIT": "512m",
    "PG_SHARED_BUFFERS": "128MB",
    "PG_EFFECTIVE_CACHE_SIZE": "384MB",
    "PG_MAINTENANCE_WORK_MEM": "64MB",
    "PG_MAX_CONNECTIONS": "50",
    "PG_WORK_MEM": "2MB",
    "PG_AUTOVACUUM_WORK_MEM": "32MB",
}


def test_test_stack_gets_small_postgres_profile(tmp_path: Path) -> None:
    # prod values in the source are overridden, missing ones are added
    source = _write_source(tmp_path, postgres_db="shop")
    with source.open("a", encoding="utf-8") as fh:
        fh.write("PG_MEM_LIMIT=4g\nPG_SHARED_BUFFERS=1GB\nPG_MAX_CONNECTIONS=200\n")
    target = tmp_path / "dst.env"

    result = _run(source, target)

    assert result.returncode == 0, result.stderr
    values = _values(target)
    assert {k: values[k] for k in _TEST_PG_PROFILE} == _TEST_PG_PROFILE
