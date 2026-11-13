"""``ops/scripts/obs_auth.sh`` and its use by ``env_test.sh --init`` (Caddy Basic Auth)."""

from __future__ import annotations

import base64
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENV_TEST = ROOT / "ops" / "scripts" / "env_test.sh"
OBS_AUTH = ROOT / "ops" / "scripts" / "obs_auth.sh"
OBS_MODE = ROOT / "ops" / "scripts" / "obs_mode.sh"

pytestmark = [
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required"),
    pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is required"),
]

_TOOLS = (
    "realpath",
    "mktemp",
    "dirname",
    "basename",
    "chmod",
    "rm",
    "ln",
    "tr",
    "openssl",
    "sed",
    "tail",
    "awk",
    "grep",
    "stat",
    "mv",
    "cat",
    "base64",
    "head",
    "printf",
)


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(
            ["docker", "info"],  # noqa: S607
            capture_output=True,
            check=False,
            timeout=30,
        ).returncode
        == 0
    )


# Valid bcrypt hash (cost 14) as printed by `caddy hash-password`.
_FAKE_BCRYPT = "$2a$14$nyeBAV95Aw4ZPS40kgnt8uA78PFEC2v6cioHXzqoAWwYAwgfBprh6"
_PASSWORD_RE = re.compile(r"user=admin password=(\S+)")


def _fake_docker_path(tmp_path: Path) -> str:
    """PATH with a fake ``docker``: ``info`` succeeds, ``run ... hash-password`` prints bcrypt."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  info) exit 0 ;;\n"
        f"  run) cat >/dev/null; printf '%s\\n' '{_FAKE_BCRYPT}' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return f"{bindir}:/usr/local/bin:/usr/bin:/bin"


def _template(tmp_path: Path) -> Path:
    src = tmp_path / "tpl.env"
    src.write_text(
        "POSTGRES_PASSWORD=change_me\nOBS_AUTH_USER=admin\nOBS_AUTH_HASH=\nOTHER=1\n",
        encoding="utf-8",
    )
    return src


def _values(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def test_init_without_docker_leaves_hash_empty(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in _TOOLS:
        found = shutil.which(tool)
        if found is not None:
            (bindir / tool).symlink_to(found)
    target = tmp_path / "out.env"
    result = subprocess.run(  # noqa: S603 - fixed script path, arguments are tmp files
        [
            shutil.which("bash") or "bash",
            str(ENV_TEST),
            "--init",
            str(_template(tmp_path)),
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=120,
        env={"PATH": str(bindir)},
    )

    assert result.returncode == 0, result.stderr
    values = _values(target)
    assert values["OBS_AUTH_HASH"] == ""
    assert values["OBS_AUTH_USER"] == "admin"
    assert "make obs-auth" in result.stdout + result.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_obs_auth_generated_password_exit_code_zero(tmp_path: Path) -> None:
    """Generation without a tty (make obs-auth in CI) must exit 0 once the hash is written."""
    env = tmp_path / "x.env"
    env.write_text("A=1\nOBS_AUTH_USER=admin\nOBS_AUTH_HASH=\n", encoding="utf-8")
    env.chmod(0o600)
    with open(os.devnull, encoding="utf-8") as devnull:
        result = subprocess.run(  # noqa: S603 - fixed script path
            ["bash", str(OBS_AUTH), str(env)],  # noqa: S607
            stdin=devnull,
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT,
            timeout=60,
            env={"PATH": _fake_docker_path(tmp_path), "OBS_AUTH_IMPLICIT": "1"},
        )

    assert result.returncode == 0, result.stderr
    values = _values(env)
    assert base64.b64decode(values["OBS_AUTH_HASH"]).decode() == _FAKE_BCRYPT
    match = _PASSWORD_RE.search(result.stderr)
    assert match is not None, result.stderr
    password = match.group(1)
    assert result.stderr.count(password) == 1
    assert password not in result.stdout
    assert password not in env.read_text(encoding="utf-8")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_init_with_docker_generates_hash_without_warning(tmp_path: Path) -> None:
    target = tmp_path / "out.env"
    result = subprocess.run(  # noqa: S603 - fixed script path, arguments are tmp files
        ["bash", str(ENV_TEST), "--init", str(_template(tmp_path)), str(target)],  # noqa: S607
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=60,
        env={"PATH": _fake_docker_path(tmp_path)},
    )

    assert result.returncode == 0, result.stderr
    assert "WARNING: OBS_AUTH_HASH is empty" not in result.stdout + result.stderr
    assert "Generated Caddy Basic Auth" in result.stdout
    assert base64.b64decode(_values(target)["OBS_AUTH_HASH"]).decode() == _FAKE_BCRYPT


@pytest.mark.skipif(not _docker_ok(), reason="docker is required")
def test_obs_auth_writes_base64_bcrypt(tmp_path: Path) -> None:
    env = tmp_path / "x.env"
    env.write_text("A=1\n# comment\nOBS_AUTH_USER=\nOBS_AUTH_HASH=old\nB=2\n", encoding="utf-8")
    env.chmod(0o600)
    password = "test-password-123456"  # noqa: S105 - test value
    result = subprocess.run(  # noqa: S603 - fixed script path
        ["bash", str(OBS_AUTH), str(env)],  # noqa: S607
        input=password + "\n",
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=180,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PASSWORD_STDIN": "1"},
    )

    assert result.returncode == 0, result.stderr
    text = env.read_text(encoding="utf-8")
    values = _values(env)
    decoded = base64.b64decode(values["OBS_AUTH_HASH"]).decode()
    assert decoded.startswith(("$2a$", "$2b$", "$2y$"))
    assert values["OBS_AUTH_USER"] == "admin"
    assert "$" not in text
    assert password not in text
    assert password not in result.stdout + result.stderr  # own password is not echoed
    assert text.startswith("A=1\n# comment\n") and text.endswith("B=2\n")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            {"CADDY_MODE": "ip", "VPS_IP": "203.0.113.5", "CADDY_HTTPS_PORT_HOST": "443"},
            "GRAFANA_ROOT_URL=https://203.0.113.5:443/grafana/ GRAFANA_SERVE_FROM_SUB_PATH=true "
            "GLITCHTIP_PUBLIC_URL=https://203.0.113.5:443",
        ),
        (
            {
                "CADDY_MODE": "domain",
                "GRAFANA_DOMAIN": "grafana.1.2.3.4.nip.io",
                "GLITCHTIP_DOMAIN": "gt.1.2.3.4.nip.io",
                "CADDY_HTTPS_PORT_HOST": "8443",
            },
            "GRAFANA_ROOT_URL=https://grafana.1.2.3.4.nip.io:8443/ "
            "GRAFANA_SERVE_FROM_SUB_PATH=false "
            "GLITCHTIP_PUBLIC_URL=https://gt.1.2.3.4.nip.io:8443",
        ),
    ],
)
def test_obs_mode_urls(env: dict[str, str], expected: str) -> None:
    result = subprocess.run(  # noqa: S603
        ["bash", str(OBS_MODE)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", **env},
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


def test_obs_mode_requires_caddy_mode_except_lenient() -> None:
    """Old .env without CADDY_MODE: no silent ip default, except for obs-down/ps/logs."""

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["bash", str(OBS_MODE), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "GRAFANA_DOMAIN": "g.example.com"},
        )

    assert run().returncode != 0
    lenient = run("--lenient")
    assert lenient.returncode == 0, lenient.stderr
    assert "CADDY_MODE=ip" in lenient.stdout


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "CADDY_MODE is not set"),
        ({"CADDY_MODE": "", "GRAFANA_DOMAIN": "g.example.com"}, "Обновление существующего .env"),
        ({"CADDY_MODE": "bogus"}, "expected ip or domain"),
        ({"CADDY_MODE": "ip"}, "requires VPS_IP"),
        ({"CADDY_MODE": "ip", "VPS_IP": "example.com"}, "not an IPv4"),
        ({"CADDY_MODE": "domain", "GRAFANA_DOMAIN": "g.example.com"}, "requires GRAFANA_DOMAIN"),
        ({"CADDY_MODE": "ip", "VPS_IP": "127.0.0.1", "OBS_AUTH_USER": "admin"}, "make obs-auth"),
        (
            {
                "CADDY_MODE": "ip",
                "VPS_IP": "127.0.0.1",
                "OBS_AUTH_USER": "admin",
                "OBS_AUTH_HASH": "bm9wZQ==",
            },
            "base64 of a bcrypt",
        ),
    ],
)
def test_obs_mode_check_rejects(env: dict[str, str], message: str) -> None:
    result = subprocess.run(  # noqa: S603
        ["bash", str(OBS_MODE), "--check"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", **env},
    )
    assert result.returncode != 0
    assert message in result.stderr
