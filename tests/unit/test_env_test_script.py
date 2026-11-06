"""``ops/scripts/env_test.sh``: the test env must point at a ``*_test`` database."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "scripts" / "env_test.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")


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
