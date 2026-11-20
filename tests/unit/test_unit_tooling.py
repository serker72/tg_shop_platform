"""Regression tests: wait_ready stdin, make check without uv, Makefile, Dockerfile."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"
DOCKERFILE = ROOT / "Dockerfile"
WAIT_READY = ROOT / "ops" / "scripts" / "wait_ready.sh"

_needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="make is required")
_needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("timeout") is None,
    reason="bash and timeout are required",
)
_needs_proc = pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="/proc is required")
# FROM [--platform=...] <image> [AS <name>]
_FROM_RE = re.compile(r"^FROM\s+(?:--\S+\s+)*\S+(?:\s+AS\s+(\S+))?", re.IGNORECASE)

# Stub of `docker`: the probe of wait_ready.sh must get stdin=/dev/null. An inherited TTY stdin
# makes `docker compose exec` (run by `timeout` in a background process group) stop on SIGTTIN.
_DOCKER_STUB = """#!/usr/bin/env bash
[ -t 0 ] && { echo "stdin is a TTY" >&2; exit 3; }
[ "$(readlink /proc/self/fd/0)" = /dev/null ] || { echo "stdin is not /dev/null" >&2; exit 3; }
printf '%s\\n' "$*" >> "$STUB_LOG"
echo '{"status": "ok"}'
"""


def _code_lines(path: Path) -> list[str]:
    """Lines of a Makefile/Dockerfile without comments."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if not line.lstrip().startswith("#")]


def test_wait_ready_probe_has_no_stdin_and_kill_timeout() -> None:
    probe = [line for line in _code_lines(WAIT_READY) if "exec -T" in line]
    assert len(probe) == 1
    assert "timeout -k " in probe[0]
    assert "</dev/null" in probe[0]


@_needs_bash
@_needs_proc
def test_wait_ready_probe_stdin_is_dev_null(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(_DOCKER_STUB, encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "docker.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "STUB_LOG": str(log),
        "READY_TIMEOUT": "0",
        "READY_INTERVAL": "0",
    }
    # stdin of the script is a pipe (not /dev/null): the probe must not inherit it
    res = subprocess.run(  # noqa: S603
        ["bash", str(WAIT_READY), "--env-file", "x.env"],  # noqa: S607
        input="",
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert "ready: master_bot bot_worker" in res.stdout
    calls = log.read_text(encoding="utf-8")
    assert "compose --env-file x.env exec -T master_bot python -c" in calls
    assert "compose --env-file x.env exec -T bot_worker python -c" in calls


def _make_dry_run(target: str, tmp_path: Path) -> str:
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    res = subprocess.run(  # noqa: S603
        ["make", "-n", target, f"ENV_FILE={env_file}"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


@_needs_make
@pytest.mark.parametrize("target", ["check", "test-unit", "test"])
def test_unit_targets_do_not_need_uv(target: str, tmp_path: Path) -> None:
    out = _make_dry_run(target, tmp_path)
    assert not re.search(r"\buv ", out), out
    assert "--target test" in out
    assert ':/app:ro"' in out
    assert "--init" in out
    assert "--iidfile" in out


@_needs_make
@pytest.mark.parametrize("target", ["test-unit-dev", "check-dev"])
def test_dev_targets_use_uv(target: str, tmp_path: Path) -> None:
    assert "uv run pytest" in _make_dry_run(target, tmp_path)


def test_unexport_unban_precedes_any_shell_call() -> None:
    """GNU make >= 4.4 passes exported (expanded) command-line vars to every $(shell)."""
    code = _code_lines(MAKEFILE)
    unexport = next(
        (i for i, line in enumerate(code) if line.split()[:2] == ["unexport", "UNBAN"]), None
    )
    assert unexport is not None, "`unexport UNBAN` not found in Makefile"
    first_shell = next(i for i, line in enumerate(code) if "$(shell" in line)
    assert unexport < first_shell, "`unexport UNBAN` must precede the first $(shell ...)"


def test_dockerfile_production_stage_is_last() -> None:
    """The default build target (docker-compose.yml has no `target`) is the production stage."""
    code = _code_lines(DOCKERFILE)
    matches = [(i, _FROM_RE.match(line)) for i, line in enumerate(code)]
    stages = [(i, m) for i, m in matches if m is not None]
    froms = [i for i, _ in stages]
    names = [m.group(1).lower() if m.group(1) else None for _, m in stages]
    assert "test" in names
    assert names[-1] is None
    production = "\n".join(code[froms[-1] :])
    assert "uv sync --locked --no-dev" in production
    assert 'CMD ["python", "-m", "app.entrypoints.master_bot"]' in production
