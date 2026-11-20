"""Regression: make env/env-test/init-dirs refuse root (ALLOW_ROOT=1); make env fills APP_UID/GID.

Root is emulated without privileges: a stub `id` first in PATH prints 0 for `id -u`/`id -g`
(the guard and env_test.sh resolve `id` via PATH); other calls go to the real `id`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    shutil.which("make") is None or shutil.which("bash") is None or shutil.which("openssl") is None,
    reason="make, bash and openssl are required",
)

_REAL_ID = shutil.which("id") or "/usr/bin/id"


def _env(tmp_path: Path, *, root: bool) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MAKE")}
    env.pop("ALLOW_ROOT", None)
    bin_dir = tmp_path / "bin"
    if root:
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / "id"
        stub.write_text(
            f'#!/bin/sh\ncase "$1" in -u|-g) echo 0;; *) exec {_REAL_ID} "$@";; esac\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
    # stub docker (exit 1): make env never reaches docker/network (obs_auth.sh: empty hash)
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    docker.chmod(0o755)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def _make(tmp_path: Path, *args: str, root: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["make", "-s", "--no-print-directory", "-C", str(ROOT), *args],  # noqa: S607
        capture_output=True,
        text=True,
        env=_env(tmp_path, root=root),
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=120,
    )


def _values(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


@pytest.mark.parametrize("target", ["env", "env-test", "init-dirs"])
def test_root_is_refused(tmp_path: Path, target: str) -> None:
    env_file = tmp_path / "x.env"
    res = _make(
        tmp_path,
        target,
        f"ENV_FILE={env_file}",
        f"TEST_ENV_FILE={tmp_path / 'x.env.test'}",
        root=True,
    )
    assert res.returncode != 0
    assert f"ERROR: do not run make {target} as root" in res.stderr
    assert "ALLOW_ROOT=1" in res.stderr
    assert not env_file.exists()
    assert not (tmp_path / "x.env.test").exists()


@pytest.mark.parametrize("value", ["0", "yes", "11", "1 1", " 1 ", "$(shell touch MARK)"])
def test_root_override_is_strict(tmp_path: Path, value: str) -> None:
    res = _make(tmp_path, "env", f"ENV_FILE={tmp_path / 'x.env'}", f"ALLOW_ROOT={value}", root=True)
    assert res.returncode != 0
    assert "ERROR: do not run make env as root" in res.stderr
    assert not (ROOT / "MARK").exists()


def test_allow_root_warns_and_continues(tmp_path: Path) -> None:
    env_file = tmp_path / "x.env"
    res = _make(tmp_path, "env", f"ENV_FILE={env_file}", "ALLOW_ROOT=1", root=True)
    assert res.returncode == 0, res.stderr
    assert "WARN: running as root (ALLOW_ROOT=1)" in res.stderr
    assert "WARN: APP_UID=0" in res.stderr
    values = _values(env_file)
    assert (values["APP_UID"], values["APP_GID"]) == ("0", "0")
    # env-test inherits APP_UID/APP_GID from the source file
    test_file = tmp_path / "x.env.test"
    res = _make(
        tmp_path,
        "env-test",
        f"ENV_FILE={env_file}",
        f"TEST_ENV_FILE={test_file}",
        "ALLOW_ROOT=1",
        root=True,
    )
    assert res.returncode == 0, res.stderr
    assert "ERROR: do not run make" not in res.stderr
    assert "WARN: running as root (ALLOW_ROOT=1)" in res.stderr
    test_values = _values(test_file)
    assert (test_values["APP_UID"], test_values["APP_GID"]) == ("0", "0")


def test_non_root_env_fills_app_uid_gid(tmp_path: Path) -> None:
    if os.getuid() == 0:
        pytest.skip("runs as root")
    env_file = tmp_path / "x.env"
    res = _make(tmp_path, "env", f"ENV_FILE={env_file}", root=False)
    assert res.returncode == 0, res.stderr
    assert "as root" not in res.stderr
    values = _values(env_file)
    assert (values["APP_UID"], values["APP_GID"]) == (str(os.getuid()), str(os.getgid()))
    # existing file is not touched (idempotent)
    before = env_file.read_bytes()
    res = _make(tmp_path, "env", f"ENV_FILE={env_file}", root=False)
    assert res.returncode == 0, res.stderr
    assert "SKIP" in res.stdout
    assert env_file.read_bytes() == before


def test_non_root_init_dirs_is_not_blocked(tmp_path: Path) -> None:
    if os.getuid() == 0:
        pytest.skip("runs as root")
    data = tmp_path / "data"
    env_file = tmp_path / "x.env"
    env_file.write_text(
        f"APP_UID={os.getuid()}\nAPP_GID={os.getgid()}\nPROJECT_DATA_DIR={data}\n"
        + "".join(
            f"{k}={data}/{d}\n"
            for k, d in {
                "POSTGRES_DATA_DIR": "db",
                "REDIS_DATA_DIR": "redis",
                "RABBITMQ_DATA_DIR": "rabbitmq",
                "MINIO_DATA_DIR": "minio",
                "LOGS_DATA_DIR": "logs",
            }.items()
        ),
        encoding="utf-8",
    )
    res = _make(tmp_path, "init-dirs", f"ENV_FILE={env_file}", root=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "as root" not in res.stderr
    assert (data / "logs" / "system" / "tmp").is_dir()


@pytest.mark.parametrize(("sub", "recursive"), [("obs/loki", True), ("redis", False)])
def test_init_dirs_hint_chowns_only_failing_dir(tmp_path: Path, sub: str, recursive: bool) -> None:
    if os.getuid() == 0:
        pytest.skip("root ignores directory permissions")
    data = tmp_path / "data"
    dirs = {"POSTGRES_DATA_DIR": "db", "REDIS_DATA_DIR": "redis/x", "RABBITMQ_DATA_DIR": "rabbitmq"}
    dirs |= {"MINIO_DATA_DIR": "minio", "LOGS_DATA_DIR": "logs", "OBS_LOKI_DATA_DIR": "obs/loki"}
    env_file = tmp_path / "x.env"
    env_file.write_text(
        f"APP_UID={os.getuid() + 1}\nAPP_GID={os.getgid()}\nPROJECT_DATA_DIR={data}\n"
        + "".join(f"{k}={data}/{d}\n" for k, d in dirs.items()),
        encoding="utf-8",
    )
    locked = data / sub
    locked.mkdir(parents=True)
    locked.chmod(0o555)  # like root:root 755 for a non-root user
    try:
        res = _make(tmp_path, "init-dirs", f"ENV_FILE={env_file}", root=False)
    finally:
        locked.chmod(0o755)
    assert res.returncode != 0
    flag = "-R " if recursive else ""
    assert f"sudo chown {flag}{os.getuid() + 1}:{os.getgid()} {locked}" in res.stderr
    assert f"chown -R {os.getuid() + 1}:{os.getgid()} {data} " not in res.stderr
    assert "Если установка выполнена под root" in res.stderr


def test_init_dirs_hint_creates_missing_data_root(tmp_path: Path) -> None:
    if os.getuid() == 0:
        pytest.skip("root ignores directory permissions")
    parent = tmp_path / "opt"
    parent.mkdir()
    parent.chmod(0o555)  # like /opt for a non-root user
    data = parent / "data"
    env_file = tmp_path / "x.env"
    env_file.write_text(
        f"APP_UID={os.getuid()}\nAPP_GID={os.getgid()}\nPROJECT_DATA_DIR={data}\n"
        + "".join(
            f"{k}={data}/{k.lower()}\n"
            for k in (
                "POSTGRES_DATA_DIR",
                "REDIS_DATA_DIR",
                "RABBITMQ_DATA_DIR",
                "MINIO_DATA_DIR",
                "LOGS_DATA_DIR",
            )
        ),
        encoding="utf-8",
    )
    try:
        res = _make(tmp_path, "init-dirs", f"ENV_FILE={env_file}", root=False)
    finally:
        parent.chmod(0o755)
    assert res.returncode != 0
    assert f"sudo install -d -o {os.getuid()} -g {os.getgid()} {data}" in res.stderr


def test_init_dirs_hint_quotes_path_with_spaces(tmp_path: Path) -> None:
    if os.getuid() == 0:
        pytest.skip("root ignores directory permissions")
    data = tmp_path / "my data"
    env_file = tmp_path / "x.env"
    dirs = ("POSTGRES_DATA_DIR", "REDIS_DATA_DIR", "RABBITMQ_DATA_DIR", "MINIO_DATA_DIR")
    env_file.write_text(
        f"APP_UID={os.getuid()}\nAPP_GID={os.getgid()}\nPROJECT_DATA_DIR='{data}'\n"
        + "".join(f"{k}='{data}/{k.lower()}'\n" for k in dirs)
        + f"LOGS_DATA_DIR='{data}/logs'\n",
        encoding="utf-8",
    )
    logs = data / "logs"
    logs.mkdir(parents=True)
    logs.chmod(0o555)
    try:
        res = _make(tmp_path, "init-dirs", f"ENV_FILE={env_file}", root=False)
    finally:
        logs.chmod(0o755)
    assert res.returncode != 0
    quoted = str(logs).replace(" ", "\\ ")
    assert f"sudo chown -R {os.getuid()}:{os.getgid()} {quoted}\n" in res.stderr
    assert "Run as root/administrator (sudo)" in res.stderr


def test_init_dirs_names_requested_goal_not_env(tmp_path: Path) -> None:
    # default ENV_FILE=.env: init-dirs runs the env prerequisite first; the guard stops there
    # (before touching .env) but must name the goal the user asked for
    res = _make(tmp_path, "init-dirs", root=True)
    assert res.returncode != 0
    assert "ERROR: do not run make init-dirs as root" in res.stderr
    assert "make init-dirs ALLOW_ROOT=1" in res.stderr
    assert "make env as root" not in res.stderr


def test_top_level_goal_is_named_for_prerequisite(tmp_path: Path) -> None:
    # test-integration -> env-test prerequisite: fails at once on the guard (no infrastructure)
    res = _make(
        tmp_path,
        "test-integration",
        f"ENV_FILE={tmp_path / 'x.env'}",
        f"TEST_ENV_FILE={tmp_path / 'x.env.test'}",
        root=True,
    )
    assert res.returncode != 0
    assert "ERROR: do not run make test-integration as root" in res.stderr
    assert "make test-integration ALLOW_ROOT=1" in res.stderr


def test_sub_make_keeps_top_level_goal(tmp_path: Path) -> None:
    # what `$(MAKE) init-dirs ENV_FILE=.env.test` of test-integration sees: inherited goal
    env = _env(tmp_path, root=True) | {"MAKELEVEL": "1", "ROOT_GUARD_GOAL": "test-integration"}
    res = subprocess.run(  # noqa: S603
        ["make", "-s", "-C", str(ROOT), "init-dirs", f"ENV_FILE={tmp_path / 'x.env.test'}"],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=120,
    )
    assert res.returncode != 0
    assert "ERROR: do not run make test-integration as root" in res.stderr


def test_goal_name_with_shell_is_not_executed(tmp_path: Path) -> None:
    marker = tmp_path / "MARK"
    res = _make(tmp_path, "init-dirs", f"$(shell touch {marker})", root=True)
    assert res.returncode != 0
    assert "ERROR: do not run make init-dirs (shell touch" in res.stderr
    assert not marker.exists()
