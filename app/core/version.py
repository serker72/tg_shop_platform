"""Application version ``X.Y.{milestone}.{commit number in milestone}`` (PEP 440).

Resolution order (the first non-empty wins, nothing here ever raises):

1. ``APP_VERSION`` / ``APP_COMMIT`` / ``APP_BUILD_TIME`` from the environment — baked into the
   image at build time (``Dockerfile`` build args, filled by ``ops/scripts/version.sh --env``);
2. git of the source checkout (only when ``.git`` and the ``git`` binary exist; each call has
   a timeout): ``{base}.{rev-list --count milestone-N..HEAD + 1}[+dirty]``;
3. ``{base}+unknown`` — ``base`` is ``[project] version`` of ``pyproject.toml``.

Same scheme as ``ops/scripts/version.sh`` (see ``docs/ops/versioning.md``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

__all__ = [
    "PROJECT_SLUG",
    "VersionInfo",
    "compute_version_info",
    "get_build_time",
    "get_commit",
    "get_version",
    "get_version_info",
    "read_base_version",
]

#: Prefix of the Sentry release (``tg-shop-platform@<version>``).
PROJECT_SLUG = "tg-shop-platform"

_ROOT = Path(__file__).resolve().parents[2]
_GIT_TIMEOUT_SECONDS = 2.0
_BASE_RE = re.compile(r"^\d+\.\d+\.(\d+)$")
_FALLBACK_BASE = "0.0.0"


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """Version of the running code."""

    version: str
    commit: str | None
    build_time: str | None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


def read_base_version(root: Path = _ROOT) -> str | None:
    """``[project] version`` of ``root/pyproject.toml`` or ``None``."""
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    return version if isinstance(version, str) and version else None


def _git(root: Path, *args: str) -> str | None:
    if not (root / ".git").exists() or shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), *args],  # noqa: S607 - git from PATH by design
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _git_version(root: Path, base: str) -> str | None:
    match = _BASE_RE.match(base)
    if match is None:
        return None
    tag = f"milestone-{match.group(1)}"
    if _git(root, "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}") is None:
        return None
    count = _git(root, "rev-list", "--count", f"{tag}..HEAD")
    if count is None or not count.isdigit():
        return None
    version = f"{base}.{int(count) + 1}"
    status = _git(root, "status", "--porcelain")
    if status is None:
        return f"{version}+unknown"
    return f"{version}+dirty" if status else version


def _env(environ: Mapping[str, str], key: str) -> str | None:
    return environ.get(key, "").strip() or None


def compute_version_info(
    root: Path = _ROOT, environ: Mapping[str, str] | None = None
) -> VersionInfo:
    """Resolve the version without caching (see the module docstring for the order)."""
    env = os.environ if environ is None else environ
    commit = _env(env, "APP_COMMIT")
    build_time = _env(env, "APP_BUILD_TIME")
    version = _env(env, "APP_VERSION")
    if version is not None:
        return VersionInfo(version=version, commit=commit, build_time=build_time)
    base = read_base_version(root)
    git_version = _git_version(root, base) if base is not None else None
    if commit is None:
        commit = _git(root, "rev-parse", "--short", "HEAD") or None
    return VersionInfo(
        version=git_version or f"{base or _FALLBACK_BASE}+unknown",
        commit=commit,
        build_time=build_time,
    )


@lru_cache(maxsize=1)
def get_version_info() -> VersionInfo:
    """Cached version of the process; never raises."""
    try:
        return compute_version_info()
    except Exception:  # noqa: BLE001 - version lookup must never break the process
        return VersionInfo(version=f"{_FALLBACK_BASE}+unknown", commit=None, build_time=None)


def get_version() -> str:
    return get_version_info().version


def get_commit() -> str | None:
    return get_version_info().commit


def get_build_time() -> str | None:
    return get_version_info().build_time
