"""Unit tests of ``app.modules.logging.retention`` (no database)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
import pydantic
import pytest

from app.modules.logging import retention
from app.modules.logging.retention import (
    AUDIT_TABLE,
    EXIT_CONFIG,
    EXIT_ERROR,
    USER_ACTIVITY_TABLE,
    InvalidRetentionError,
    PartConfigMissingError,
    PartConfigRow,
    PartmanRetentionGuardError,
    SystemSettingsMissingError,
    check_audit_guard,
    exit_code_for,
    format_retention,
    plan_changes,
    sync_retention,
    validate_retention_months,
)


def _audit(keep_table: bool | None = True, schema: str | None = "archive") -> PartConfigRow:
    return PartConfigRow(AUDIT_TABLE, "24 months", schema, keep_table, True)


def _activity() -> PartConfigRow:
    return PartConfigRow(USER_ACTIVITY_TABLE, "3 months", None, False, True)


@pytest.mark.parametrize("value", [1, 120, 3, 24])
def test_validate_retention_ok(value: int) -> None:
    assert validate_retention_months("f", value) == value


@pytest.mark.parametrize("value", [0, 121, -1, "5", 5.0, None, True])
def test_validate_retention_rejects(value: object) -> None:
    with pytest.raises(InvalidRetentionError):
        validate_retention_months("f", value)


def test_format_retention() -> None:
    assert format_retention(5) == "5 months"
    assert format_retention(120) == "120 months"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (PartmanRetentionGuardError("x"), EXIT_CONFIG),
        (SystemSettingsMissingError("x"), EXIT_CONFIG),
        (InvalidRetentionError("x"), EXIT_CONFIG),
        (PartConfigMissingError("x"), EXIT_CONFIG),
        (psycopg.OperationalError("down"), EXIT_ERROR),
        (RuntimeError("x"), EXIT_ERROR),
    ],
)
def test_exit_code_for(error: BaseException, code: int) -> None:
    assert exit_code_for(error) == code


def test_exit_code_for_settings_validation_error() -> None:
    class Model(pydantic.BaseModel):
        port: int

    with pytest.raises(pydantic.ValidationError) as caught:
        Model.model_validate({"port": "abc"})
    assert exit_code_for(caught.value) == EXIT_CONFIG


@pytest.mark.parametrize(
    ("keep_table", "schema"), [(False, "archive"), (None, "archive"), (True, None)]
)
def test_guard_rejects_bad_audit_config(keep_table: bool | None, schema: str | None) -> None:
    with pytest.raises(PartmanRetentionGuardError, match="PARTMAN_RETENTION_GUARD:"):
        check_audit_guard(_audit(keep_table, schema))


def test_guard_ignores_user_activity() -> None:
    check_audit_guard(_activity())
    check_audit_guard(_audit())


def test_plan_changes() -> None:
    changes = plan_changes((5, 24), [_activity(), _audit()])
    assert [(c.parent_table, c.old, c.new, c.changed) for c in changes] == [
        (USER_ACTIVITY_TABLE, "3 months", "5 months", True),
        (AUDIT_TABLE, "24 months", "24 months", False),
    ]


def test_plan_changes_missing_config_row() -> None:
    with pytest.raises(PartConfigMissingError):
        plan_changes((3, 24), [_audit()])


def test_plan_changes_invalid_value_before_guard() -> None:
    with pytest.raises(InvalidRetentionError):
        plan_changes((0, 24), [_activity(), _audit(False)])


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConnection:
    """Minimal stand-in for ``psycopg.Connection``: records statements and the outcome."""

    def __init__(self, settings: tuple[Any, ...] | None, config: list[tuple[Any, ...]]) -> None:
        self.settings = settings
        self.config = config
        self.updates: list[tuple[Any, ...]] = []
        self.statements: list[str] = []
        self.outcome: str | None = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
        except psycopg.Rollback:
            self.outcome = "rollback"
            return
        except BaseException:
            self.outcome = "rollback"
            raise
        self.outcome = "commit"

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> _Cursor:
        self.statements.append(sql)
        if sql.startswith("SELECT user_activity_retention_months"):
            return _Cursor([self.settings] if self.settings is not None else [])
        if sql.startswith("SELECT parent_table"):
            return _Cursor(self.config)
        assert sql.startswith("UPDATE partman.part_config")
        assert params is not None
        self.updates.append(params)
        return _Cursor([])


_CONFIG = [
    ("public.audit_logs", "24 months", "archive", True, True),
    ("public.user_activity", "3 months", None, False, True),
]


def _sync(fake: _FakeConnection, *, dry_run: bool = False) -> retention.SyncResult:
    return sync_retention(cast("psycopg.Connection[Any]", fake), dry_run=dry_run)


def test_sync_updates_only_changed() -> None:
    fake = _FakeConnection((5, 24), _CONFIG)
    result = _sync(fake)
    assert fake.updates == [("5 months", USER_ACTIVITY_TABLE)]
    assert fake.outcome == "commit"
    assert result.committed and [c.parent_table for c in result.updated] == [USER_ACTIVITY_TABLE]


def test_sync_noop() -> None:
    fake = _FakeConnection((3, 24), _CONFIG)
    result = _sync(fake)
    assert fake.updates == [] and not result.updated and not result.committed


def test_sync_dry_run_rolls_back() -> None:
    fake = _FakeConnection((5, 30), _CONFIG)
    result = _sync(fake, dry_run=True)
    assert fake.updates == [] and fake.outcome == "rollback"
    assert len(result.updated) == 2 and not result.committed


def test_sync_guard_writes_nothing() -> None:
    config = [("public.audit_logs", "24 months", "archive", False, True), _CONFIG[1]]
    fake = _FakeConnection((5, 30), config)
    with pytest.raises(PartmanRetentionGuardError):
        _sync(fake)
    assert fake.updates == [] and fake.outcome == "rollback"


def test_sync_missing_settings_row() -> None:
    fake = _FakeConnection(None, _CONFIG)
    with pytest.raises(SystemSettingsMissingError):
        _sync(fake)
    assert fake.updates == []


def test_sync_missing_part_config_rows_exit_2() -> None:
    fake = _FakeConnection((3, 24), [])
    with pytest.raises(PartConfigMissingError) as caught:
        _sync(fake)
    assert exit_code_for(caught.value) == EXIT_CONFIG


def test_sync_locks_part_config_before_reading_settings() -> None:
    """RL1: ``FOR UPDATE`` on part_config precedes the system_settings read."""
    fake = _FakeConnection((5, 30), _CONFIG)
    _sync(fake)
    lock = next(i for i, sql in enumerate(fake.statements) if "partman.part_config" in sql)
    read = next(i for i, sql in enumerate(fake.statements) if "system_settings" in sql)
    assert fake.statements[lock].startswith("SELECT parent_table")
    assert fake.statements[lock].rstrip().endswith("FOR UPDATE")
    assert lock < read
    assert all(sql.startswith("UPDATE") for sql in fake.statements[read + 1 :])


_SECRET = "unit-secret-pw-7f3a"


def _run_main_with_connect_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception
) -> tuple[int, str]:
    def fake_connect(**kwargs: Any) -> Any:
        conninfo = f"password={kwargs['password']}"  # a local holding the secret
        assert conninfo
        raise error

    monkeypatch.setenv("POSTGRES_PASSWORD", _SECRET)
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    code = retention.main([])
    out = capsys.readouterr()
    return code, out.out + out.err


def test_main_unexpected_error_logs_traceback_without_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """RL3: unknown errors -> exit 1 with a traceback, but no password (no frame locals)."""
    code, output = _run_main_with_connect_error(monkeypatch, capsys, RuntimeError("boom"))
    assert code == EXIT_ERROR
    assert "partman_retention_sync_failed" in output
    assert "Traceback" in output and "fake_connect" in output
    assert _SECRET not in output


def test_main_operational_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    error = psycopg.OperationalError("connection refused")
    code, output = _run_main_with_connect_error(monkeypatch, capsys, error)
    assert code == EXIT_ERROR
    assert "OperationalError" in output and "Traceback" not in output
    assert _SECRET not in output
