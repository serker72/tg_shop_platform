"""CrowdSec + Caddy catch-all of the observability stack (configs, env generation, guards)."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CROWDSEC = ROOT / "ops" / "observability" / "crowdsec"
CADDY = ROOT / "ops" / "observability" / "caddy"
ENV_TEST = ROOT / "ops" / "scripts" / "env_test.sh"
OBS_MODE = ROOT / "ops" / "scripts" / "obs_mode.sh"

pytestmark = pytest.mark.req("SEC1")

_needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
_FAKE_BCRYPT = "$2a$14$nyeBAV95Aw4ZPS40kgnt8uA78PFEC2v6cioHXzqoAWwYAwgfBprh6"


def test_crowdsec_yaml_files_parse() -> None:
    files = sorted(CROWDSEC.rglob("*.yaml"))
    assert files
    for path in files:
        assert yaml.safe_load(path.read_text(encoding="utf-8")), path


def test_whitelist_covers_own_networks() -> None:
    doc = yaml.safe_load(
        (CROWDSEC / "parsers" / "s02-enrich" / "tgsp-whitelist.yaml").read_text(encoding="utf-8")
    )
    cidr = set(doc["whitelist"]["cidr"])
    assert {"127.0.0.0/8", "172.16.0.0/12", "10.10.0.0/24"} <= cidr


def test_obs_mode_hint_does_not_push_force() -> None:
    text = OBS_MODE.read_text(encoding="utf-8")
    assert "openssl rand -hex 32" in text
    assert "only for a NEW installation" in text


@pytest.mark.parametrize("mode", ["ip", "domain"])
def test_caddyfile_catch_all_and_bouncer(mode: str) -> None:
    text = (CADDY / f"Caddyfile.{mode}").read_text(encoding="utf-8")
    assert re.search(r"^http:// \{\n(\t.*\n)*?\tabort\n\}", text, re.MULTILINE)
    assert "api_key {$CROWDSEC_BOUNCER_KEY}" in text
    assert "order crowdsec first" in text
    assert "/var/log/caddy/access.log" in text


@_needs_bash
@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is required")
def test_init_generates_bouncer_key(tmp_path: Path) -> None:
    src = tmp_path / "example"
    src.write_text("CROWDSEC_BOUNCER_KEY=\nOTHER=\n", encoding="utf-8")
    dst = tmp_path / ".env"
    subprocess.run(  # noqa: S603
        ["bash", str(ENV_TEST), "--init", str(src), str(dst)],  # noqa: S607
        check=True,
        capture_output=True,
    )
    text = dst.read_text(encoding="utf-8")
    assert re.search(r"^CROWDSEC_BOUNCER_KEY=[0-9a-f]{64}$", text, re.MULTILINE)
    assert "OTHER=\n" in text


def _obs_mode_env(**extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ["PATH"],
        "CADDY_MODE": "ip",
        "VPS_IP": "127.0.0.1",
        "OBS_AUTH_USER": "admin",
        "OBS_AUTH_HASH": base64.b64encode(_FAKE_BCRYPT.encode()).decode(),
    }
    env.update(extra)
    return env


@_needs_bash
@pytest.mark.parametrize("key", ["", "short", "z" * 64])
def test_obs_mode_check_requires_bouncer_key(key: str) -> None:
    res = subprocess.run(  # noqa: S603
        ["bash", str(OBS_MODE), "--check"],  # noqa: S607
        env=_obs_mode_env(CROWDSEC_BOUNCER_KEY=key),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1
    assert "CROWDSEC_BOUNCER_KEY" in res.stderr


@_needs_bash
def test_obs_mode_check_accepts_bouncer_key() -> None:
    res = subprocess.run(  # noqa: S603
        ["bash", str(OBS_MODE), "--check"],  # noqa: S607
        env=_obs_mode_env(CROWDSEC_BOUNCER_KEY="ab" * 32),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr


@_needs_bash
@pytest.mark.skipif(shutil.which("make") is None, reason="make is required")
def test_obs_bans_unban_is_not_executed(tmp_path: Path) -> None:
    """UNBAN reaches the recipe only via the environment: shell syntax is never executed."""
    env_file = tmp_path / ".env"
    env_file.write_text("CADDY_MODE=ip\nVPS_IP=127.0.0.1\n", encoding="utf-8")
    marker = tmp_path / "pwned"
    res = subprocess.run(  # noqa: S603
        ["make", "-s", "obs-bans", f"ENV_FILE={env_file}", f"UNBAN=$(touch {marker})"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert "not an IP address" in res.stderr
    assert not marker.exists()


@pytest.mark.parametrize("mode", ["ip", "domain"])
def test_caddyfile_https_catch_all_logged(mode: str) -> None:
    text = (CADDY / f"Caddyfile.{mode}").read_text(encoding="utf-8")
    block = re.search(r"^https:// \{\n(.*?)^\}", text, re.MULTILINE | re.DOTALL)
    assert block
    assert "import access_log" in block.group(1)
    assert "abort" in block.group(1)
    assert "mode 0640" in text


@pytest.mark.skipif(shutil.which("make") is None, reason="make is required")
@pytest.mark.parametrize("dry_run", [False, True])
def test_obs_bans_unban_make_shell_not_expanded(tmp_path: Path, dry_run: bool) -> None:
    """``$(shell ...)`` in UNBAN must not run (command-line vars are expanded on export)."""
    env_file = tmp_path / ".env"
    env_file.write_text("CADDY_MODE=ip\nVPS_IP=127.0.0.1\n", encoding="utf-8")
    marker = tmp_path / "pwn_make"
    args = ["make", "-s", *(["-n"] if dry_run else []), "obs-bans", f"ENV_FILE={env_file}"]
    subprocess.run(  # noqa: S603
        [*args, f"UNBAN=$(shell touch {marker})"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert not marker.exists()
