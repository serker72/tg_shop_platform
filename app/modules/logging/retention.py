"""Sync ``partman.part_config.retention`` with ``public.system_settings`` (singleton ``id=1``).

Entry point: ``ops/scripts/sync_partman_retention.py`` (``db_update`` after
``alembic upgrade head``, or manually after a settings change). The logic lives here so it
is type-checked and unit-testable; the script only fixes ``sys.path`` and calls :func:`main`.

One transaction:

1. read retention months from ``system_settings`` (no row -> :class:`SystemSettingsMissingError`);
2. validate them (``int``, 1..120) and build ``"<N> months"``;
3. lock both ``part_config`` rows (``FOR UPDATE``);
4. guard (first line of defence): ``public.audit_logs`` must keep detached tables in a schema
   (``retention_keep_table IS TRUE AND retention_schema IS NOT NULL``), otherwise
   :class:`PartmanRetentionGuardError` and nothing is written;
5. ``UPDATE ... SET retention`` only for changed rows (values are bound parameters);
6. COMMIT (``--dry-run``: rollback).

The database trigger ``guard_audit_part_config`` is the second line of defence: its
``check_violation`` with the ``PARTMAN_RETENTION_GUARD:`` prefix is converted into
:class:`PartmanRetentionGuardError` as well.
"""

from __future__ import annotations

import argparse
import os
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import psycopg
import pydantic
from psycopg import errors as pg_errors

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

__all__ = [
    "AUDIT_TABLE",
    "EXIT_CONFIG",
    "EXIT_ERROR",
    "EXIT_OK",
    "GUARD_PREFIX",
    "MAX_RETENTION_MONTHS",
    "MIN_RETENTION_MONTHS",
    "USER_ACTIVITY_TABLE",
    "InvalidRetentionError",
    "SELECT_PART_CONFIG",
    "PartConfigMissingError",
    "PartConfigRow",
    "PartmanRetentionGuardError",
    "RetentionChange",
    "RetentionSyncError",
    "SyncResult",
    "SystemSettingsMissingError",
    "check_audit_guard",
    "exit_code_for",
    "format_retention",
    "is_db_guard_error",
    "main",
    "plan_changes",
    "sync_retention",
    "validate_retention_months",
]

EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_CONFIG: Final = 2

MIN_RETENTION_MONTHS: Final = 1
MAX_RETENTION_MONTHS: Final = 120

USER_ACTIVITY_TABLE: Final = "public.user_activity"
AUDIT_TABLE: Final = "public.audit_logs"
GUARD_PREFIX: Final = "PARTMAN_RETENTION_GUARD:"

#: ``part_config.parent_table`` -> ``system_settings`` column.
SETTINGS_COLUMNS: Final[Mapping[str, str]] = {
    USER_ACTIVITY_TABLE: "user_activity_retention_months",
    AUDIT_TABLE: "audit_logs_retention_months",
}

_SELECT_SETTINGS: Final = (
    "SELECT user_activity_retention_months, audit_logs_retention_months "
    "FROM public.system_settings WHERE id = 1"
)
#: Locks both log ``part_config`` rows; take this lock BEFORE reading ``system_settings``.
SELECT_PART_CONFIG: Final = (
    "SELECT parent_table, retention, retention_schema, retention_keep_table, "
    "retention_keep_index FROM partman.part_config "
    "WHERE parent_table IN ('public.user_activity', 'public.audit_logs') "
    "ORDER BY parent_table FOR UPDATE"
)
_UPDATE_RETENTION: Final = "UPDATE partman.part_config SET retention = %s WHERE parent_table = %s"

logger = get_logger(__name__)


class RetentionSyncError(Exception):
    """Configuration/guard problem: the sync refuses to write (exit code 2)."""


class SystemSettingsMissingError(RetentionSyncError):
    """``public.system_settings`` has no singleton row ``id = 1``."""


class InvalidRetentionError(RetentionSyncError):
    """A retention value is not an ``int`` in ``[1, 120]``."""


class PartConfigMissingError(RetentionSyncError):
    """``partman.part_config`` lacks a row for one of the log tables."""


class PartmanRetentionGuardError(RetentionSyncError):
    """``public.audit_logs`` config would drop partitions instead of archiving them."""


@dataclass(frozen=True, slots=True)
class PartConfigRow:
    """Retention-related columns of one ``partman.part_config`` row."""

    parent_table: str
    retention: str | None
    retention_schema: str | None
    retention_keep_table: bool | None
    retention_keep_index: bool | None


@dataclass(frozen=True, slots=True)
class RetentionChange:
    """Old and new ``retention`` of one parent table."""

    parent_table: str
    old: str | None
    new: str

    @property
    def changed(self) -> bool:
        return self.old != self.new


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Outcome of :func:`sync_retention`; ``committed`` is false for dry runs."""

    changes: tuple[RetentionChange, ...]
    dry_run: bool

    @property
    def updated(self) -> tuple[RetentionChange, ...]:
        return tuple(change for change in self.changes if change.changed)

    @property
    def committed(self) -> bool:
        return not self.dry_run and bool(self.updated)


def validate_retention_months(field: str, value: object) -> int:
    """Return ``value`` if it is an ``int`` (not ``bool``) in ``[1, 120]``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRetentionError(f"{field} must be an integer, got {type(value).__name__}")
    if not MIN_RETENTION_MONTHS <= value <= MAX_RETENTION_MONTHS:
        raise InvalidRetentionError(
            f"{field}={value} is out of range [{MIN_RETENTION_MONTHS}, {MAX_RETENTION_MONTHS}]"
        )
    return value


def format_retention(months: int) -> str:
    """pg_partman retention interval text: strictly ``"<N> months"``."""
    return f"{months} months"


def check_audit_guard(row: PartConfigRow) -> None:
    """First line of defence for ``public.audit_logs`` (design-plan, "Guard")."""
    if row.parent_table != AUDIT_TABLE:
        return
    if row.retention_keep_table is not True or row.retention_schema is None:
        raise PartmanRetentionGuardError(
            f"{GUARD_PREFIX} {AUDIT_TABLE} requires retention_keep_table = true and "
            f"retention_schema IS NOT NULL (got retention_keep_table="
            f"{row.retention_keep_table}, retention_schema={row.retention_schema})"
        )


def plan_changes(
    settings_row: Sequence[Any], config_rows: Sequence[PartConfigRow]
) -> tuple[RetentionChange, ...]:
    """Validate inputs, apply the guard and compute the per-table retention plan.

    ``settings_row`` is ``(user_activity_retention_months, audit_logs_retention_months)``.
    Raises before anything is written: invalid value, missing config row or guard violation.
    """
    months = {
        table: validate_retention_months(column, settings_row[index])
        for index, (table, column) in enumerate(SETTINGS_COLUMNS.items())
    }
    by_table = {row.parent_table: row for row in config_rows}
    missing = sorted(set(SETTINGS_COLUMNS) - set(by_table))
    if missing:
        raise PartConfigMissingError(f"partman.part_config has no row for {', '.join(missing)}")
    for row in by_table.values():
        check_audit_guard(row)
    return tuple(
        RetentionChange(table, by_table[table].retention, format_retention(months[table]))
        for table in SETTINGS_COLUMNS
    )


def is_db_guard_error(error: psycopg.Error) -> bool:
    """``True`` for the ``check_violation`` of the ``guard_audit_part_config`` trigger."""
    if not isinstance(error, pg_errors.CheckViolation):
        return False
    primary = error.diag.message_primary or str(error)
    return primary.startswith(GUARD_PREFIX)


def sync_retention(conn: psycopg.Connection[Any], *, dry_run: bool = False) -> SyncResult:
    """Run the sync in one transaction on ``conn`` (rolled back on error or ``dry_run``)."""
    try:
        with conn.transaction():
            # Lock order matters (READ COMMITTED): take the ``part_config`` row locks FIRST and
            # only then read ``system_settings``. Every statement gets a fresh snapshot, so the
            # settings read after the lock see everything committed before the lock was
            # granted; concurrent syncs serialise on the lock and the last writer always
            # writes the latest settings. Reading settings before the lock would let a stale
            # sync (read S1 -> wait for lock -> write S1) overwrite a newer S2.
            config_rows = [
                PartConfigRow(
                    parent_table=str(row[0]),
                    retention=row[1],
                    retention_schema=row[2],
                    retention_keep_table=row[3],
                    retention_keep_index=row[4],
                )
                for row in conn.execute(SELECT_PART_CONFIG).fetchall()
            ]
            settings_row = conn.execute(_SELECT_SETTINGS).fetchone()
            if settings_row is None:
                raise SystemSettingsMissingError("public.system_settings has no row with id = 1")
            changes = plan_changes(settings_row, config_rows)
            for change in changes:
                if not change.changed:
                    logger.info(
                        "partman_retention_unchanged", table=change.parent_table, value=change.new
                    )
                    continue
                logger.info(
                    "partman_retention_update",
                    table=change.parent_table,
                    old=change.old,
                    new=change.new,
                    dry_run=dry_run,
                )
                if not dry_run:
                    conn.execute(_UPDATE_RETENTION, (change.new, change.parent_table))
            if dry_run:
                raise psycopg.Rollback()
    except psycopg.Error as error:
        if is_db_guard_error(error):
            raise PartmanRetentionGuardError(
                error.diag.message_primary or str(error).strip()
            ) from error
        raise
    return SyncResult(changes=changes, dry_run=dry_run)


def exit_code_for(error: BaseException) -> int:
    """``2`` for guard/configuration problems, ``1`` for anything else."""
    if isinstance(error, RetentionSyncError | pydantic.ValidationError):
        return EXIT_CONFIG
    return EXIT_ERROR


def _first_line(error: BaseException) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else ""


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: ``python ops/scripts/sync_partman_retention.py [--dry-run]``; returns the exit code."""
    parser = argparse.ArgumentParser(
        prog="sync_partman_retention",
        description="Sync partman.part_config.retention with public.system_settings.",
    )
    parser.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    args = parser.parse_args(argv)
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        pg = get_settings().postgres
        if pg.password is None:
            raise RetentionSyncError("POSTGRES_PASSWORD is not configured")
        with psycopg.connect(
            host=pg.host,
            port=pg.port,
            dbname=pg.db,
            user=pg.user,
            password=pg.password.get_secret_value(),
            connect_timeout=10,
            application_name="sync_partman_retention",
        ) as conn:
            result = sync_retention(conn, dry_run=args.dry_run)
    except Exception as error:
        code = exit_code_for(error)
        details: dict[str, Any] = {}
        if code == EXIT_ERROR and not isinstance(error, psycopg.OperationalError):
            # Unexpected error: keep the traceback. Not ``exc_info=True``: the shared
            # ``dict_tracebacks`` processor renders frame locals, and the locals of
            # ``psycopg.connect`` hold the password. ``format_exception`` has no locals.
            details["traceback"] = "".join(traceback.format_exception(error))
        logger.error(
            "partman_retention_sync_failed",
            error_type=type(error).__name__,
            error=_first_line(error),
            exit_code=code,
            **details,
        )
        return code
    logger.info(
        "partman_retention_sync_done",
        dry_run=result.dry_run,
        updated=[change.parent_table for change in result.updated],
        committed=result.committed,
    )
    return EXIT_OK
