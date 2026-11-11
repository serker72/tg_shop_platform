"""Versioning: ops/scripts/version.sh, app.core.version, /version, Sentry release, build info."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sentry_sdk
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web import Application
from prometheus_client import CollectorRegistry
from pydantic import SecretStr

from app.core.config import ObservabilitySettings, Settings
from app.core.observability.health import HealthChecker
from app.core.observability.http_server import create_app
from app.core.observability.metrics import build_metrics, render_metrics, set_build_info
from app.core.observability.sentry import init_sentry
from app.core.version import (
    VersionInfo,
    compute_version_info,
    get_version,
    get_version_info,
    read_base_version,
)

SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "version.sh"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, **_GIT_ENV}
    out = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(repo), *args],  # noqa: S607
        env=env,
        check=True,
        capture_output=True,
        text=True,  # noqa: S607 - git from PATH
    )
    return out.stdout.strip()


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Repo with base 0.1.1: one pre-milestone commit, tag milestone-1 on commit 2, +2 commits."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.other]\nversion = "9.9.9"\n\n[project]\nname = "x"\nversion = "0.1.1"  # base\n'
    )
    _git(tmp_path, "add", "pyproject.toml")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _commit(tmp_path, "a")
    _git(tmp_path, "tag", "-a", "milestone-1", "-m", "Milestone 1 start")
    _commit(tmp_path, "b")
    _commit(tmp_path, "c")
    return tmp_path


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed script path
        ["sh", str(SCRIPT), *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,  # noqa: S607 - sh from PATH
    )


# --- ops/scripts/version.sh -------------------------------------------------------------


@needs_git
def test_script_counts_commits_since_milestone_tag(repo: Path) -> None:
    result = _run(repo)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.1.1.3"


@needs_git
def test_script_tagged_commit_is_one(repo: Path) -> None:
    _git(repo, "checkout", "-q", "milestone-1")
    assert _run(repo).stdout.strip() == "0.1.1.1"


@needs_git
def test_script_dirty_suffix(repo: Path) -> None:
    (repo / "a").write_text("changed")
    assert _run(repo).stdout.strip() == "0.1.1.3+dirty"


@needs_git
def test_script_untracked_file_is_dirty(repo: Path) -> None:
    (repo / "new.txt").write_text("x")
    assert _run(repo).stdout.strip() == "0.1.1.3+dirty"


@needs_git
def test_script_missing_tag_fails_with_hint(repo: Path) -> None:
    _git(repo, "tag", "-d", "milestone-1")
    result = _run(repo)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "milestone-1 not found" in result.stderr
    assert "git tag -a milestone-1" in result.stderr


@needs_git
def test_script_commit_and_env(repo: Path) -> None:
    sha = _git(repo, "rev-parse", "--short", "HEAD")
    assert _run(repo, "--commit").stdout.strip() == sha
    lines = _run(repo, "--env").stdout.splitlines()
    assert lines[0] == "APP_VERSION=0.1.1.3"
    assert lines[1] == f"APP_COMMIT={sha}"
    assert re.fullmatch(r"APP_BUILD_TIME=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", lines[2])


@needs_git
def test_script_rejects_bad_base_and_option(repo: Path) -> None:
    assert _run(repo, "--bogus").returncode == 2
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.1"\n')
    result = _run(repo)
    assert result.returncode == 1
    assert "must be X.Y.N" in result.stderr


def test_scripts_are_shellcheck_clean() -> None:
    """``version.sh`` and the other ``ops/scripts/*.sh`` pass shellcheck (skip without it)."""
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed")
    scripts = sorted(str(path) for path in SCRIPT.parent.glob("*.sh"))
    assert str(SCRIPT) in scripts
    result = subprocess.run(  # noqa: S603 - absolute shellcheck path, repo script paths
        [shellcheck, *scripts], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout


# --- app.core.version -------------------------------------------------------------------


def test_env_wins(tmp_path: Path) -> None:
    info = compute_version_info(
        tmp_path,
        {
            "APP_VERSION": "0.1.1.7",
            "APP_COMMIT": "abc1234",
            "APP_BUILD_TIME": "2025-01-01T00:00:00Z",
        },
    )
    assert info == VersionInfo("0.1.1.7", "abc1234", "2025-01-01T00:00:00Z")


@needs_git
def test_git_version_matches_script(repo: Path) -> None:
    info = compute_version_info(repo, {})
    assert info.version == _run(repo).stdout.strip() == "0.1.1.3"
    assert info.commit == _git(repo, "rev-parse", "--short", "HEAD")
    assert info.build_time is None
    (repo / "a").write_text("changed")
    assert compute_version_info(repo, {}).version == "0.1.1.3+dirty"


@needs_git
def test_missing_tag_falls_back_to_unknown(repo: Path) -> None:
    _git(repo, "tag", "-d", "milestone-1")
    assert compute_version_info(repo, {}).version == "0.1.1+unknown"


def test_no_git_falls_back_to_base(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.4"\n')
    assert read_base_version(tmp_path) == "0.1.4"
    assert compute_version_info(tmp_path, {}) == VersionInfo("0.1.4+unknown", None, None)


def test_no_pyproject_never_fails(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("not = [valid")
    assert compute_version_info(tmp_path, {"APP_VERSION": " "}).version == "0.0.0+unknown"


def test_get_version_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    get_version_info.cache_clear()
    monkeypatch.setenv("APP_VERSION", "0.1.1.42")
    try:
        assert get_version() == "0.1.1.42"
        monkeypatch.setenv("APP_VERSION", "0.1.1.43")
        assert get_version() == "0.1.1.42"
    finally:
        get_version_info.cache_clear()


# --- /version, app_build_info, Sentry release --------------------------------------------


async def _get_json(app: Application, path: str) -> tuple[int, Any]:
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(path)
        return resp.status, await resp.json()


def test_version_endpoint() -> None:
    info = VersionInfo("0.1.1.5", "abc1234", "2025-01-01T00:00:00Z")
    app = create_app(
        cast(HealthChecker, object()),
        CollectorRegistry(),
        component="master_bot",
        environment="dev",
        version_info=info,
    )
    status, body = asyncio.run(_get_json(app, "/version"))
    assert status == 200
    assert body == {
        "version": "0.1.1.5",
        "commit": "abc1234",
        "build_time": "2025-01-01T00:00:00Z",
        "component": "master_bot",
        "environment": "dev",
    }


def test_build_info_metric() -> None:
    metrics = build_metrics()
    set_build_info(
        metrics, component="bot_worker", version_info=VersionInfo("0.1.1.5", "abc1234", None)
    )
    text = render_metrics(metrics.registry).decode()
    lines = [line for line in text.splitlines() if line.startswith("app_build_info{")]
    assert lines == [
        'app_build_info{commit="abc1234",component="bot_worker",version="0.1.1.5"} 1.0'
    ]


def _settings(**obs: Any) -> Settings:
    observability = ObservabilitySettings(
        _env_file=None,  # type: ignore[call-arg]
        sentry_dsn=SecretStr("http://public@glitchtip.invalid:8000/1"),
        **obs,
    )
    return cast(Settings, SimpleNamespace(observability=observability, project_environment="dev"))


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        (None, "tg-shop-platform@0.1.1.9"),
        ("custom@1", "custom@1"),
        ("", "tg-shop-platform@0.1.1.9"),
    ],
)
def test_init_sentry_release(
    monkeypatch: pytest.MonkeyPatch, release: str | None, expected: str
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda *a: None)
    monkeypatch.delenv("SENTRY_RELEASE", raising=False)
    monkeypatch.setenv("APP_VERSION", "0.1.1.9")
    get_version_info.cache_clear()
    try:
        kwargs = {} if release is None else {"sentry_release": release}
        assert init_sentry(_settings(**kwargs), component="test") is True
    finally:
        get_version_info.cache_clear()
    assert captured["release"] == expected
