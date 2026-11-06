"""Tests for the Alembic configuration (no database connection is used).

The checks guarantee that ``alembic.ini`` stays free of credentials, that the script
location and ``versions/`` directory exist, and that the migration history is a single
linear chain with exactly one head.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


@pytest.fixture(name="alembic_config")
def alembic_config_fixture() -> Config:
    """Return a :class:`alembic.config.Config` built from the project ``alembic.ini``."""
    assert ALEMBIC_INI.is_file(), f"{ALEMBIC_INI} is missing"
    return Config(str(ALEMBIC_INI))


@pytest.fixture(name="alembic_ini")
def alembic_ini_fixture() -> configparser.ConfigParser:
    """Parse ``alembic.ini`` keeping duplicate-key errors visible."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ALEMBIC_INI, encoding="utf-8")
    return parser


def test_alembic_ini_has_no_database_url(alembic_ini: configparser.ConfigParser) -> None:
    """The URL must come from ``Settings.database_url``, never from the ini file."""
    assert not alembic_ini.has_option("alembic", "sqlalchemy.url")

    raw = ALEMBIC_INI.read_text(encoding="utf-8")
    credential_markers = ("postgresql://", "postgresql+psycopg://", "postgresql+asyncpg://")
    for marker in credential_markers:
        assert marker not in raw, f"alembic.ini must not contain {marker!r}"


def test_script_location_points_to_existing_directory(
    alembic_ini: configparser.ConfigParser, alembic_config: Config
) -> None:
    """``script_location`` and the ``versions`` directory exist next to the ini file."""
    script_location = alembic_config.get_main_option("script_location")
    assert script_location == "migrations"

    scripts_dir = PROJECT_ROOT / str(script_location)
    assert scripts_dir.is_dir()
    assert (scripts_dir / "env.py").is_file()
    assert (scripts_dir / "script.py.mako").is_file()
    assert (scripts_dir / "versions").is_dir()
    assert script_location == alembic_ini.get("alembic", "script_location")


def test_single_head_with_empty_base_revision(alembic_config: Config) -> None:
    """The history has exactly one head and a single parentless base revision."""
    script = ScriptDirectory.from_config(alembic_config)

    heads = script.get_heads()
    assert len(heads) == 1, heads

    history = list(script.walk_revisions(base="base", head="heads"))
    roots = [rev for rev in history if rev.down_revision is None]
    assert [rev.revision for rev in roots] == ["initial"], [
        (rev.revision, rev.down_revision) for rev in history
    ]
    assert history[0].revision == heads[0]
    assert all(not rev.is_merge_point for rev in history)


def test_revision_files_are_readable(alembic_config: Config) -> None:
    """``file_template`` produces a date-prefixed, rev-containing file name."""
    script = ScriptDirectory.from_config(alembic_config)
    head = script.get_current_head()
    assert head is not None

    revision = script.get_revision(head)
    file_name = Path(revision.path).name
    assert head in file_name
    assert file_name[:8].isdigit(), file_name
