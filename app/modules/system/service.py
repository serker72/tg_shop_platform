"""``SystemSettingsService``: write use cases of the ``system_settings`` singleton (R-II.5).

Every mutating method follows the IAM write-service pattern (``app.modules.iam.admin_service``):

1. ``PermissionService.require(actor, settings.manage)`` before any write;
2. validation of the range (the CHECK constraints of the table mirror it);
3. ``set_retention`` only: lock ``partman.part_config`` rows FIRST (``FOR UPDATE``, the lock
   order of :func:`app.modules.logging.retention.sync_retention`), then the settings row,
   then update ``part_config.retention`` in the same transaction (guard -> rollback);
4. an ``AuditLogger`` record in the SAME transaction (``before`` / ``after``);
5. ``commit`` (any error -> rollback, nothing is written);
6. strictly AFTER the commit: ``PermissionService.invalidate_all`` (IAM cache TTL) or a reset
   of the process-local :class:`MaintenanceGate` cache. Other processes (client bots) read
   the flag again within the gate TTL.

A request that changes nothing commits the read transaction and writes neither audit nor
invalidation (``_noop``). After every call :attr:`cache_invalidated` tells whether Redis
confirmed ``invalidate_all`` (``False`` -> stored, stale permission TTL until expiry).

**Session contract.** The service commits the request session itself, so it must not hold
foreign pending changes on entry (``session_not_clean``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final

import structlog
from psycopg import Error as PsycopgError
from psycopg import errors as pg_errors
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam.metrics import (
    CacheInvalidationFailures,
    iam_cache_invalidation_failures_total,
)
from app.modules.iam.permissions import SETTINGS_MANAGE
from app.modules.iam.service import PermissionService
from app.modules.logging import retention
from app.modules.logging.audit import SqlAuditLogger
from app.modules.logging.events import AuditEvent
from app.modules.logging.ports import AuditLogger
from app.modules.system.maintenance import MaintenanceGate
from app.modules.system.models import (
    FILE_LOGS_RETENTION_DAYS_MAX,
    FILE_LOGS_RETENTION_DAYS_MIN,
    RETENTION_MONTHS_MAX,
    RETENTION_MONTHS_MIN,
    SystemSettings,
)
from app.modules.system.repository import SystemSettingsRepository
from app.shared_kernel.domain.exceptions import DomainError, ValidationError

__all__ = [
    "IAM_CACHE_TTL_SECONDS_MAX",
    "IAM_CACHE_TTL_SECONDS_MIN",
    "INVALID_FILE_LOGS_RETENTION",
    "INVALID_IAM_CACHE_TTL",
    "INVALID_MAINTENANCE_FLAG",
    "INVALID_RETENTION",
    "LOCK_TIMEOUT_MS",
    "RETENTION_CONFIG_ERROR",
    "RETENTION_GUARD",
    "SETTINGS_BUSY",
    "SESSION_NOT_CLEAN",
    "SystemSettingsService",
    "validate_file_logs_days",
    "validate_iam_cache_ttl",
    "validate_retention_months",
]

INVALID_FILE_LOGS_RETENTION: Final = "invalid_file_logs_retention"
INVALID_IAM_CACHE_TTL: Final = "invalid_iam_cache_ttl"
INVALID_MAINTENANCE_FLAG: Final = "invalid_maintenance_flag"
INVALID_RETENTION: Final = "invalid_retention"
RETENTION_GUARD: Final = "retention_guard"
#: ``part_config`` rows missing / invalid stored value (not the audit_logs guard).
RETENTION_CONFIG_ERROR: Final = "retention_config_error"
#: A row lock was not granted within :data:`LOCK_TIMEOUT_MS` (``lock_not_available``).
SETTINGS_BUSY: Final = "settings_busy"
SESSION_NOT_CLEAN: Final = "session_not_clean"
#: ``SET LOCAL lock_timeout`` of every write transaction: a long ``part_config`` /
#: ``system_settings`` lock holder must not hang the bot handler.
LOCK_TIMEOUT_MS: Final = 5000

#: IAM snapshot TTL bounds (seconds): 1 s .. 30 days (the table CHECK only demands > 0).
IAM_CACHE_TTL_SECONDS_MIN: Final = 1
IAM_CACHE_TTL_SECONDS_MAX: Final = 30 * 24 * 60 * 60

_ENTITY: Final = "system_settings"
_UPDATE_RETENTION = text(
    "UPDATE partman.part_config SET retention = :retention WHERE parent_table = :parent_table"
)
_log = structlog.get_logger(__name__)


class _NoChange(Exception):  # noqa: N818 - internal control-flow signal
    """The requested state already holds."""


def _int_in(value: object, low: int, high: int, *, code: str, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValidationError(f"{field} must be an integer in [{low}, {high}]", code=code)
    return value


def validate_retention_months(value: object, field: str = "retention_months") -> int:
    return _int_in(
        value, RETENTION_MONTHS_MIN, RETENTION_MONTHS_MAX, code=INVALID_RETENTION, field=field
    )


def validate_file_logs_days(value: object) -> int:
    return _int_in(
        value,
        FILE_LOGS_RETENTION_DAYS_MIN,
        FILE_LOGS_RETENTION_DAYS_MAX,
        code=INVALID_FILE_LOGS_RETENTION,
        field="file_logs_retention_days",
    )


def validate_iam_cache_ttl(value: object) -> int:
    return _int_in(
        value,
        IAM_CACHE_TTL_SECONDS_MIN,
        IAM_CACHE_TTL_SECONDS_MAX,
        code=INVALID_IAM_CACHE_TTL,
        field="iam_cache_ttl_seconds",
    )


def _state(settings: SystemSettings) -> dict[str, Any]:
    return {
        "is_maintenance_mode": settings.is_maintenance_mode,
        "iam_cache_ttl_seconds": settings.iam_cache_ttl_seconds,
        "user_activity_retention_months": settings.user_activity_retention_months,
        "audit_logs_retention_months": settings.audit_logs_retention_months,
        "file_logs_retention_days": settings.file_logs_retention_days,
    }


class SystemSettingsService:
    """Write service of the platform settings; bound to one request session (sequential use)."""

    def __init__(
        self,
        session: AsyncSession,
        permissions: PermissionService,
        *,
        gate: MaintenanceGate | None = None,
        audit: AuditLogger | None = None,
        invalidation_failures: CacheInvalidationFailures = iam_cache_invalidation_failures_total,
        lock_timeout_ms: int = LOCK_TIMEOUT_MS,
    ) -> None:
        self._lock_timeout_ms = int(lock_timeout_ms)
        self._session = session
        self._permissions = permissions
        self._gate = gate
        self._audit_logger: AuditLogger = audit or SqlAuditLogger()
        self._repo = SystemSettingsRepository(session)
        self._failures = invalidation_failures
        self._noop = False
        self.cache_invalidated = True

    # --- reads -----------------------------------------------------------------------------

    async def get(self) -> SystemSettings:
        """Current settings (read access is checked by the caller: panel getters)."""
        return await self._repo.get()

    # --- writes ----------------------------------------------------------------------------

    async def set_maintenance(self, actor_telegram_id: int, enabled: bool) -> SystemSettings:
        if not isinstance(enabled, bool):
            raise ValidationError("enabled must be bool", code=INVALID_MAINTENANCE_FLAG)
        settings = await self._change(
            actor_telegram_id, "system.settings.maintenance", {"is_maintenance_mode": enabled}
        )
        if not self._noop and self._gate is not None:
            self._gate.invalidate()
        return settings

    async def set_iam_cache_ttl(self, actor_telegram_id: int, seconds: int) -> SystemSettings:
        value = validate_iam_cache_ttl(seconds)
        settings = await self._change(
            actor_telegram_id, "system.settings.iam_cache_ttl", {"iam_cache_ttl_seconds": value}
        )
        if not self._noop:
            if not (await self._permissions.invalidate_all()).ok:
                self.cache_invalidated = False
                self._failures.inc(scope="all")
                _log.error("system_settings_cache_invalidation_failed")
        return settings

    async def set_retention(
        self,
        actor_telegram_id: int,
        user_activity_months: int | None = None,
        audit_logs_months: int | None = None,
    ) -> SystemSettings:
        """Store the given retention value(s) and sync ``partman.part_config`` in one
        transaction. ``None`` keeps the column: its value comes from the row read under
        ``FOR UPDATE`` (no lost update of a concurrent change of the other column)."""
        values: dict[str, int] = {}
        if user_activity_months is not None:
            values["user_activity_retention_months"] = validate_retention_months(
                user_activity_months, "user_activity_months"
            )
        if audit_logs_months is not None:
            values["audit_logs_retention_months"] = validate_retention_months(
                audit_logs_months, "audit_logs_months"
            )
        if not values:
            raise ValidationError(
                "user_activity_months or audit_logs_months is required", code=INVALID_RETENTION
            )
        return await self._change(
            actor_telegram_id, "system.settings.retention", values, sync_partman=True
        )

    async def set_file_logs_retention_days(
        self, actor_telegram_id: int, days: int
    ) -> SystemSettings:
        value = validate_file_logs_days(days)
        return await self._change(
            actor_telegram_id,
            "system.settings.file_logs_retention",
            {"file_logs_retention_days": value},
        )

    # --- internals -------------------------------------------------------------------------

    async def _change(
        self,
        actor_telegram_id: int,
        action: str,
        values: Mapping[str, Any],
        *,
        sync_partman: bool = False,
    ) -> SystemSettings:
        async with self._transaction():
            await self._session.execute(
                text(f"SET LOCAL lock_timeout = '{self._lock_timeout_ms}ms'")
            )
            await self._permissions.require(actor_telegram_id, SETTINGS_MANAGE)
            config_rows = await self._lock_part_config() if sync_partman else []
            settings = await self._repo.get_for_update()
            before = _state(settings)
            for key, value in values.items():
                setattr(settings, key, value)
            after = _state(settings)
            synced: list[dict[str, Any]] = []
            if sync_partman:
                synced = await self._sync_partman(settings, config_rows)
            if before == after and not synced:
                raise _NoChange
            await self._session.flush()
            await self._audit_logger.record(
                self._session,
                AuditEvent(
                    actor_type="user",
                    action=action,
                    tenant_id=None,
                    actor_id=actor_telegram_id,
                    entity_type=_ENTITY,
                    entity_id=None,
                    payload={
                        "before": {k: before[k] for k in values},
                        "after": {k: after[k] for k in values},
                        **({"partman": synced} if synced else {}),
                    },
                    result="success",
                ),
            )
        return settings

    async def _lock_part_config(self) -> list[retention.PartConfigRow]:
        result = await self._session.execute(text(retention.SELECT_PART_CONFIG))
        return [
            retention.PartConfigRow(
                parent_table=str(row[0]),
                retention=row[1],
                retention_schema=row[2],
                retention_keep_table=row[3],
                retention_keep_index=row[4],
            )
            for row in result.all()
        ]

    async def _sync_partman(
        self, settings: SystemSettings, rows: list[retention.PartConfigRow]
    ) -> list[dict[str, Any]]:
        """Guarded plan of :mod:`app.modules.logging.retention`, applied in this transaction."""
        try:
            changes = retention.plan_changes(
                (settings.user_activity_retention_months, settings.audit_logs_retention_months),
                rows,
            )
        except retention.PartmanRetentionGuardError as error:
            raise DomainError(str(error), code=RETENTION_GUARD) from error
        except retention.RetentionSyncError as error:
            # PartConfigMissingError / InvalidRetentionError: broken config, not the guard.
            raise DomainError(str(error), code=RETENTION_CONFIG_ERROR) from error
        synced = []
        for change in changes:
            if not change.changed:
                continue
            await self._session.execute(
                _UPDATE_RETENTION,
                {"retention": change.new, "parent_table": change.parent_table},
            )
            synced.append({"table": change.parent_table, "old": change.old, "new": change.new})
        return synced

    def _ensure_clean_session(self) -> None:
        for name in ("new", "dirty", "deleted"):
            if getattr(self._session, name, None):
                raise ValidationError(
                    "Session holds foreign pending changes", code=SESSION_NOT_CLEAN
                )

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        self._ensure_clean_session()
        self._noop = False
        self.cache_invalidated = True
        try:
            yield
            await self._session.commit()
        except _NoChange:
            await self._session.commit()
            self._noop = True
        except DBAPIError as error:
            await self._session.rollback()
            orig = error.orig
            if isinstance(orig, pg_errors.LockNotAvailable):
                raise DomainError(
                    "system settings are locked by another transaction", code=SETTINGS_BUSY
                ) from error
            if isinstance(orig, PsycopgError) and retention.is_db_guard_error(orig):
                raise DomainError(
                    "partman retention guard rejected the change", code=RETENTION_GUARD
                ) from error
            raise
        except BaseException:
            await self._session.rollback()
            raise
