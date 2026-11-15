"""``make superuser``: parameter validation and the generated command (``make -n``, no Docker)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make is required")

_PARAMS = ("TG_ID", "TG_USERNAME", "ACTION", "FORCE", "USERNAME", "MAKEFLAGS", "MFLAGS")


def make_superuser(
    *params: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k not in _PARAMS}
    base.update(env or {})
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["make", "--no-print-directory", "-n", "superuser", "ENV_FILE=.env.example", *params],  # noqa: S607
        cwd=ROOT,
        env=base,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def cli_line(proc: subprocess.CompletedProcess[str]) -> str:
    assert proc.returncode == 0, proc.stderr
    # ``make -n`` prints the recipe with its ``\`` line continuations.
    return " ".join(proc.stdout.replace("\\\n", " ").split())


def test_create_default_ignores_os_username() -> None:
    line = cli_line(make_superuser("TG_ID=5", env={"USERNAME": "serg"}))
    assert "python -m app.entrypoints.cli.superuser create --telegram-id '5'" in line
    assert "--username" not in line
    assert "serg" not in line


def test_create_with_username() -> None:
    line = cli_line(make_superuser("TG_ID=5", "TG_USERNAME=@Boss_1"))
    assert "create --telegram-id '5' --username '@Boss_1'" in line


@pytest.mark.parametrize(
    ("force", "expected"), [("0", False), ("", False), ("1", True), ("yes", False)]
)
def test_revoke_force_only_literal_one(force: str, expected: bool) -> None:
    line = cli_line(make_superuser("TG_ID=7", "ACTION=revoke", f"FORCE={force}"))
    assert "superuser revoke --telegram-id '7'" in line
    assert ("--force" in line) is expected


def test_revoke_ignores_username() -> None:
    line = cli_line(make_superuser("TG_ID=7", "ACTION=revoke", "TG_USERNAME=bob"))
    assert "--username" not in line


def test_create_never_forces() -> None:
    assert "--force" not in cli_line(make_superuser("TG_ID=7", "FORCE=1"))


@pytest.mark.parametrize(
    "params",
    [
        (),
        ("TG_ID=",),
        ("TG_ID=abc",),
        ("TG_ID=-5",),
        ("TG_ID=5;id",),
        ("TG_ID=5 6",),
        ("TG_ID='5'",),
        ("TG_ID=5", "TG_USERNAME=a'b"),
        ("TG_ID=5", "TG_USERNAME=a;id"),
        ("TG_ID=5", "TG_USERNAME=a b"),
        ("TG_ID=5", "TG_USERNAME=$$(id)"),
        ("TG_ID=5", "ACTION=delete"),
        ("TG_ID=5", "ACTION=0"),
        ("TG_ID=5", "ACTION=create revoke"),
    ],
)
def test_invalid_parameters_exit_2(params: tuple[str, ...]) -> None:
    proc = make_superuser(*params)
    assert proc.returncode == 2, proc.stdout
    assert "docker" not in proc.stdout
    assert "superuser" not in proc.stdout


def test_listed_in_help() -> None:
    proc = subprocess.run(
        ["make", "--no-print-directory", "help"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    assert "TG_ID=<id>" in proc.stdout
