"""``sync_partman_retention.py`` / ``migrate.sh`` against scratch databases (real COMMITs).

Scripts run as real subprocesses with ``POSTGRES_*`` of the scratch database in the
environment (the password never appears in argv). SQL is composed from ints only, hence S608.
"""

# ruff: noqa: S608

from __future__ import annotations

import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import make_url

from app.core.config import Settings
from app.modules.logging import retention
from app.modules.logging.retention import PartmanRetentionGuardError, sync_retention
from tests.integration._cli import cli_env as _env
from tests.integration._cli import run_cli as _run
from tests.integration.conftest import ALEMBIC_INI, ROOT, Query

pytestmark = pytest.mark.integration

SYNC_SCRIPT = ROOT / "ops" / "scripts" / "sync_partman_retention.py"
MIGRATE_SCRIPT = ROOT / "ops" / "scripts" / "migrate.sh"
RETENTION_SQL = (
    "SELECT parent_table, retention FROM partman.part_config "
    "WHERE parent_table IN ('public.user_activity', 'public.audit_logs') ORDER BY parent_table"
)


def _sync(settings: Settings, url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, str(SYNC_SCRIPT), *args], _env(settings, url))


def _intervals_equal(query: Query, user_activity: int, audit: int) -> bool:
    rows = query(
        "SELECT bool_and(CASE parent_table "
        f"WHEN 'public.user_activity' THEN retention::interval = '{user_activity} months' "
        f"WHEN 'public.audit_logs' THEN retention::interval = '{audit} months' END) "
        "FROM partman.part_config WHERE parent_table IN "
        "('public.user_activity', 'public.audit_logs')"
    )
    return bool(rows[0][0])


def _set_settings(query: Query, user_activity: int, audit: int) -> None:
    query(
        f"UPDATE system_settings SET user_activity_retention_months = {user_activity}, "
        f"audit_logs_retention_months = {audit} WHERE id = 1"
    )


def _corrupt_audit_config(query: Query) -> None:
    query("ALTER TABLE partman.part_config DISABLE TRIGGER guard_audit_part_config")
    query(
        "UPDATE partman.part_config SET retention_keep_table = false "
        "WHERE parent_table = 'public.audit_logs'"
    )
    query("ALTER TABLE partman.part_config ENABLE TRIGGER guard_audit_part_config")


def _connect(settings: Settings, url: str, **extra: Any) -> psycopg.Connection[Any]:
    pg = settings.postgres
    assert pg.password is not None
    return psycopg.connect(
        host=pg.host,
        port=pg.port,
        user=pg.user,
        password=pg.password.get_secret_value(),
        dbname=str(make_url(url).database),
        **extra,
    )


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_sync_happy_path_and_noop(
    settings: Settings, migrated_scratch_db: tuple[str, Query]
) -> None:
    url, query = migrated_scratch_db
    assert _intervals_equal(query, 3, 24)
    _set_settings(query, 5, 30)

    dry = _sync(settings, url, "--dry-run")
    assert dry.returncode == 0, dry.stderr + dry.stdout
    assert _intervals_equal(query, 3, 24)

    first = _sync(settings, url)
    assert first.returncode == 0, first.stderr + first.stdout
    assert query(RETENTION_SQL) == [
        ("public.audit_logs", "30 months"),
        ("public.user_activity", "5 months"),
    ]
    assert _intervals_equal(query, 5, 30)
    assert "partman_retention_update" in first.stdout
    assert settings.postgres.password is not None
    assert settings.postgres.password.get_secret_value() not in first.stdout + first.stderr

    second = _sync(settings, url)
    assert second.returncode == 0, second.stderr + second.stdout
    assert "partman_retention_update" not in second.stdout
    assert "partman_retention_unchanged" in second.stdout
    assert _intervals_equal(query, 5, 30)


def test_sync_s3_guard_writes_nothing(
    settings: Settings, migrated_scratch_db: tuple[str, Query]
) -> None:
    url, query = migrated_scratch_db
    _corrupt_audit_config(query)
    _set_settings(query, 5, 30)
    before = query(RETENTION_SQL)

    result = _sync(settings, url)
    assert result.returncode == 2, result.stderr + result.stdout
    assert "PartmanRetentionGuardError" in result.stdout
    assert query(RETENTION_SQL) == before
    assert _intervals_equal(query, 3, 24)


def test_sync_missing_system_settings(
    settings: Settings, migrated_scratch_db: tuple[str, Query]
) -> None:
    url, query = migrated_scratch_db
    query("DELETE FROM system_settings")
    result = _sync(settings, url)
    assert result.returncode == 2, result.stderr + result.stdout
    assert "SystemSettingsMissingError" in result.stdout
    assert _intervals_equal(query, 3, 24)


def test_sync_unreachable_database_exit_1(
    settings: Settings, migrated_scratch_db: tuple[str, Query]
) -> None:
    url, _ = migrated_scratch_db
    env = _env(settings, url)
    env["POSTGRES_DB"] = "missing_db_for_retention_test"
    result = _run([sys.executable, str(SYNC_SCRIPT)], env)
    assert result.returncode == 1, result.stderr + result.stdout


def test_db_trigger_is_second_line(
    settings: Settings,
    migrated_scratch_db: tuple[str, Query],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, query = migrated_scratch_db
    _corrupt_audit_config(query)
    _set_settings(query, 5, 30)
    monkeypatch.setattr(retention, "check_audit_guard", lambda row: None)
    with _connect(settings, url) as conn, pytest.raises(PartmanRetentionGuardError) as caught:
        sync_retention(conn)
    assert str(caught.value).startswith("PARTMAN_RETENTION_GUARD:")
    assert _intervals_equal(query, 3, 24)


def test_migrate_sh_on_empty_database(settings: Settings, scratch_db: tuple[str, Query]) -> None:
    url, query = scratch_db
    env = _env(settings, url)
    for _ in range(2):
        result = _run(["/bin/sh", str(MIGRATE_SCRIPT)], env)
        assert result.returncode == 0, result.stderr + result.stdout
        assert "[migrate] alembic upgrade head" in result.stderr
        assert "[migrate] platform setup" in result.stderr
        assert "platform_setup_done" in result.stdout
        assert "[migrate] done" in result.stderr
    head = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
    assert head is not None
    assert query("SELECT version_num FROM alembic_version") == [(head,)]
    # Setup re-creates a lost singleton row before the retention sync reads it.
    query("DELETE FROM system_settings")
    result = _run(["/bin/sh", str(MIGRATE_SCRIPT)], env)
    assert result.returncode == 0, result.stderr + result.stdout
    assert query("SELECT id FROM system_settings") == [(1,)]
    _set_settings(query, 7, 36)
    result = _run(["/bin/sh", str(MIGRATE_SCRIPT)], env)
    assert result.returncode == 0, result.stderr + result.stdout
    assert _intervals_equal(query, 7, 36)


def test_concurrent_sync_waits_for_lock_and_reads_fresh_settings(
    settings: Settings, migrated_scratch_db: tuple[str, Query]
) -> None:
    """RL1: a sync blocked on the part_config lock writes settings committed meanwhile."""
    url, query = migrated_scratch_db
    _set_settings(query, 5, 30)

    def run() -> retention.SyncResult:
        with _connect(settings, url, application_name="rl1_sync") as conn:
            return sync_retention(conn)

    with _connect(settings, url) as holder, ThreadPoolExecutor(max_workers=1) as pool:
        holder.execute(
            "SELECT 1 FROM partman.part_config WHERE parent_table IN "
            "('public.user_activity', 'public.audit_logs') FOR UPDATE"
        )
        future = pool.submit(run)
        deadline = time.monotonic() + 10
        while not query(
            "SELECT 1 FROM pg_stat_activity WHERE application_name = 'rl1_sync' "
            "AND wait_event_type = 'Lock'"
        ):
            assert not future.done(), future.result()
            assert time.monotonic() < deadline, "sync never blocked on the part_config lock"
            time.sleep(0.05)
        _set_settings(query, 7, 36)  # committed while the sync waits for the lock
        holder.commit()
        result = future.result(timeout=10)
    assert [c.new for c in result.changes] == ["7 months", "36 months"]
    assert _intervals_equal(query, 7, 36)


def test_migrate_sh_db_unreachable(
    settings: Settings, migrated_scratch_db: tuple[str, Query]
) -> None:
    """RL4: closed port -> wait_for_db times out, alembic never starts, no password."""
    url, _ = migrated_scratch_db
    env = _env(settings, url)
    env.update(
        {
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": str(_closed_port()),
            "MIGRATE_WAIT_SECONDS": "1",
        }
    )
    started = time.monotonic()
    result = _run(["/bin/sh", str(MIGRATE_SCRIPT)], env)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert time.monotonic() - started < 30
    assert "[migrate] waiting for PostgreSQL" in result.stderr
    assert "is not ready after 1s" in result.stderr
    assert "[migrate] alembic upgrade head" not in result.stderr
    assert env["POSTGRES_PASSWORD"] not in output


def test_migrate_sh_wrong_password_times_out(
    settings: Settings, migrated_scratch_db: tuple[str, Query]
) -> None:
    """RL4: a wrong password counts as "not ready" until the timeout, then exit 1."""
    url, _ = migrated_scratch_db
    env = _env(settings, url)
    env.update({"POSTGRES_PASSWORD": "definitely-wrong-pw-91c2", "MIGRATE_WAIT_SECONDS": "1"})
    result = _run(["/bin/sh", str(MIGRATE_SCRIPT)], env)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "is not ready after 1s" in result.stderr
    assert "[migrate] alembic upgrade head" not in result.stderr
    assert "definitely-wrong-pw-91c2" not in output
