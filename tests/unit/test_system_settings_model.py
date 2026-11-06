"""Unit tests of the ``SystemSettings`` singleton model (no database)."""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Table

from app.modules.system.models import (
    DEFAULT_AUDIT_LOGS_RETENTION_MONTHS,
    DEFAULT_USER_ACTIVITY_RETENTION_MONTHS,
    SystemSettings,
)

TABLE = cast(Table, SystemSettings.__table__)


def test_checks() -> None:
    checks = {
        str(c.name): str(c.sqltext) for c in TABLE.constraints if isinstance(c, CheckConstraint)
    }
    assert checks == {
        "ck_system_settings_singleton": "id = 1",
        "ck_system_settings_iam_cache_ttl_positive": "iam_cache_ttl_seconds > 0",
        "ck_system_settings_user_activity_retention_range": (
            "user_activity_retention_months BETWEEN 1 AND 120"
        ),
        "ck_system_settings_audit_logs_retention_range": (
            "audit_logs_retention_months BETWEEN 1 AND 120"
        ),
        "ck_system_settings_file_logs_retention_range": (
            "file_logs_retention_days BETWEEN 1 AND 3650"
        ),
    }


def test_id_is_fixed_integer() -> None:
    column = TABLE.c.id
    assert column.primary_key
    assert column.autoincrement is False
    assert getattr(column.default, "arg", None) == 1


def test_server_defaults() -> None:
    def server_default(name: str) -> str:
        default = TABLE.c[name].server_default
        assert default is not None
        return str(getattr(default, "arg", None))

    assert server_default("iam_cache_ttl_seconds") == "86400"
    assert server_default("extra_config") == "'{}'::jsonb"
    assert server_default("user_activity_retention_months") == "3"
    assert server_default("audit_logs_retention_months") == "24"
    assert server_default("file_logs_retention_days") == "90"
    assert DEFAULT_USER_ACTIVITY_RETENTION_MONTHS == 3
    assert DEFAULT_AUDIT_LOGS_RETENTION_MONTHS == 24
    for name in TABLE.c.keys():
        assert not TABLE.c[name].nullable


def test_timestamps_and_eager_defaults() -> None:
    assert {"created_at", "updated_at"} <= set(TABLE.c.keys())
    assert SystemSettings.__mapper__.eager_defaults is True
