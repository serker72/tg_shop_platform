"""Helpers for running project CLIs/scripts as subprocesses against a test database.

The password is passed through the environment only (never in argv).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import make_url

from app.core.config import Settings
from tests.integration.conftest import ROOT

__all__ = ["MIN_LEAK_CHECK_PASSWORD_LEN", "assert_no_password", "cli_env", "run_cli"]

#: Shorter passwords may occur in output by accident; only the ``:<pw>@`` DSN form is checked.
MIN_LEAK_CHECK_PASSWORD_LEN = 8


def cli_env(settings: Settings, url: str) -> dict[str, str]:
    """Process environment with ``POSTGRES_*`` of ``settings`` and the database of ``url``."""
    pg = settings.postgres
    assert pg.password is not None
    venv_bin = str(Path(sys.executable).parent)
    env = dict(os.environ)
    env.update(
        {
            "PROJECT_ENVIRONMENT": "test",
            "POSTGRES_HOST": pg.host,
            "POSTGRES_PORT": str(pg.port),
            "POSTGRES_USER": pg.user,
            "POSTGRES_PASSWORD": pg.password.get_secret_value(),
            "POSTGRES_DB": str(make_url(url).database),
            "MIGRATE_WAIT_SECONDS": "10",
            "PATH": f"{venv_bin}{os.pathsep}{env.get('PATH', '')}",
        }
    )
    return env


def run_cli(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run ``command`` from the project root; never raises on a non-zero exit code."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=180, check=False
    )


def assert_no_password(output: str, password: str) -> None:
    """The password must not leak: never as ``:<pw>@``, and verbatim only if long enough."""
    assert f":{password}@" not in output
    if len(password) >= MIN_LEAK_CHECK_PASSWORD_LEN:
        assert password not in output
