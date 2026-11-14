"""``ops/scripts/env_check.sh``: local env files must have every key of ``.env.example``."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "scripts" / "env_check.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed script path, arguments are tmp files
        ["bash", str(SCRIPT), *args],  # noqa: S607 - bash from PATH
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=30,
    )


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def example(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "example", "# header\nA=1\nexport B=2\n# C=optional\nSECRET=change_me\n"
    )


def test_all_keys_present(tmp_path: Path, example: Path) -> None:
    env = _write(tmp_path / "env", "A=x\r\nB=y\nSECRET=topsecretvalue\nEXTRA=1\n")

    result = _run(str(example), str(env))

    assert result.returncode == 0, result.stderr
    assert f"OK: {env}" in result.stdout


def test_missing_keys_are_reported_without_values(tmp_path: Path, example: Path) -> None:
    env = _write(tmp_path / "env", "A=topsecretvalue\n# SECRET=old\n")
    other = _write(tmp_path / "other", "A=1\nB=2\nSECRET=3\n")

    result = _run(str(example), str(env), str(other))

    assert result.returncode == 1
    assert (
        f"{env} lacks keys from {example}: B SECRET\n" in result.stderr
    )  # commented C is optional
    assert "topsecretvalue" not in result.stdout + result.stderr
    assert f"OK: {other}" in result.stdout


def test_absent_file_fails(tmp_path: Path, example: Path) -> None:
    result = _run(str(example), str(tmp_path / "nope"))

    assert result.returncode == 1
    assert "not found" in result.stderr


def test_warn_mode_never_fails(tmp_path: Path, example: Path) -> None:
    env = _write(tmp_path / "env", "A=1\n")

    result = _run("--warn", str(example), str(env), str(tmp_path / "nope"))

    assert result.returncode == 0
    assert "WARNING:" in result.stderr
