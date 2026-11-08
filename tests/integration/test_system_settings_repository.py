"""``SystemSettingsRepository`` and the setup CLI against PostgreSQL.

Repository tests use the rolled-back ``session`` fixture: ``DELETE`` of the singleton row
never leaves the outer transaction, so neighbouring tests always see the row. CLI tests
run as subprocesses against a migrated scratch database (real COMMITs).
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from sqlalchemy import delete, inspect, make_url, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.system.models import (
    DEFAULT_AUDIT_LOGS_RETENTION_MONTHS,
    DEFAULT_FILE_LOGS_RETENTION_DAYS,
    DEFAULT_IAM_CACHE_TTL_SECONDS,
    DEFAULT_USER_ACTIVITY_RETENTION_MONTHS,
    SINGLETON_ID,
    SystemSettings,
)
from app.modules.system.repository import SYSTEM_SETTINGS_MISSING, SystemSettingsRepository
from app.shared_kernel.domain.exceptions import EntityNotFoundError
from tests.integration._cli import assert_no_password, cli_env, run_cli
from tests.integration._guards import assert_test_database_name
from tests.integration.conftest import Query

pytestmark = pytest.mark.integration

SETTINGS_SQL = (
    "SELECT is_maintenance_mode, iam_cache_ttl_seconds, extra_config, "
    "user_activity_retention_months, audit_logs_retention_months, file_logs_retention_days "
    "FROM system_settings"
)
DEFAULT_ROW: tuple[object, ...] = (
    False,
    DEFAULT_IAM_CACHE_TTL_SECONDS,
    {},
    DEFAULT_USER_ACTIVITY_RETENTION_MONTHS,
    DEFAULT_AUDIT_LOGS_RETENTION_MONTHS,
    DEFAULT_FILE_LOGS_RETENTION_DAYS,
)


async def test_ensure_exists_inserts_defaults_and_never_overwrites(session: AsyncSession) -> None:
    repo = SystemSettingsRepository(session)
    await session.execute(delete(SystemSettings))

    assert await repo.ensure_exists() is True
    rows = (await session.execute(text(SETTINGS_SQL))).all()
    assert [tuple(row) for row in rows] == [DEFAULT_ROW]

    await session.execute(
        update(SystemSettings)
        .where(SystemSettings.id == SINGLETON_ID)
        .values(iam_cache_ttl_seconds=10)
    )
    assert await repo.ensure_exists() is False
    ttl = await session.scalar(select(SystemSettings.iam_cache_ttl_seconds))
    assert ttl == 10


async def test_ensure_exists_matches_server_defaults(session: AsyncSession) -> None:
    """ORM insert sends the model's Python ``default=`` values; they must equal the
    ``server_default`` values a bare ``INSERT (id)`` produces (all columns but id/timestamps).
    """
    table = SystemSettings.__table__
    columns = [c.name for c in table.columns if c.name not in {"id", "created_at", "updated_at"}]
    select_sql = text(f"SELECT {', '.join(columns)} FROM system_settings")  # noqa: S608

    await session.execute(delete(SystemSettings))
    await session.execute(
        text("INSERT INTO system_settings (id) VALUES (:id)"), {"id": SINGLETON_ID}
    )
    server_rows = [tuple(row) for row in (await session.execute(select_sql)).all()]

    await session.execute(delete(SystemSettings))
    assert await SystemSettingsRepository(session).ensure_exists() is True
    orm_rows = [tuple(row) for row in (await session.execute(select_sql)).all()]

    assert len(server_rows) == 1
    assert orm_rows == server_rows


async def test_get_missing_row(session: AsyncSession) -> None:
    await session.execute(delete(SystemSettings))
    repo = SystemSettingsRepository(session)
    for method in (repo.get, repo.get_for_update):
        with pytest.raises(EntityNotFoundError) as caught:
            await method()
        assert caught.value.code == SYSTEM_SETTINGS_MISSING


async def test_get_for_update_change_and_flush(session: AsyncSession) -> None:
    repo = SystemSettingsRepository(session)
    entity = await repo.get_for_update()
    assert entity.id == SINGLETON_ID

    entity.iam_cache_ttl_seconds = 10
    await session.flush()
    # eager_defaults: updated_at came back with RETURNING; reading it needs no IO.
    assert "updated_at" not in inspect(entity).unloaded
    assert entity.updated_at is not None
    assert (await repo.get()).iam_cache_ttl_seconds == 10


async def test_get_for_update_refreshes_identity_map(session: AsyncSession) -> None:
    repo = SystemSettingsRepository(session)
    entity = await repo.get()
    assert entity.iam_cache_ttl_seconds == DEFAULT_IAM_CACHE_TTL_SECONDS

    await session.execute(
        text("UPDATE system_settings SET iam_cache_ttl_seconds = 77 WHERE id = :id"),
        {"id": SINGLETON_ID},
    )
    locked = await repo.get_for_update()
    assert locked is entity
    assert locked.iam_cache_ttl_seconds == 77


def _setup_cli(settings: Settings, url: str) -> subprocess.CompletedProcess[str]:
    return run_cli([sys.executable, "-m", "app.entrypoints.cli.setup"], cli_env(settings, url))


def _password(settings: Settings) -> str:
    assert settings.postgres.password is not None
    return settings.postgres.password.get_secret_value()


def test_setup_cli_inserts_once(settings: Settings, migrated_scratch_db: tuple[str, Query]) -> None:
    url, query = migrated_scratch_db
    query("DELETE FROM system_settings")

    first = _setup_cli(settings, url)
    assert first.returncode == 0, first.stderr + first.stdout
    assert "platform_setup_done" in first.stdout
    assert query(SETTINGS_SQL) == [DEFAULT_ROW]
    assert_no_password(first.stdout + first.stderr, _password(settings))

    query("UPDATE system_settings SET iam_cache_ttl_seconds = 10")
    second = _setup_cli(settings, url)
    assert second.returncode == 0, second.stderr + second.stdout
    assert query("SELECT iam_cache_ttl_seconds FROM system_settings") == [(10,)]


def test_setup_cli_missing_database_exit_1(settings: Settings) -> None:
    """No migrations needed: the CLI must fail to connect to a non-existent database."""
    name = f"{settings.postgres.db}_absent_test"
    assert_test_database_name(name)
    url = make_url(settings.database_url).set(database=name).render_as_string(False)
    result = _setup_cli(settings, url)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "platform_setup_failed" in output
    assert_no_password(output, _password(settings))
