"""Unit tests of the partitioned log models (no database)."""

from __future__ import annotations

from datetime import UTC
from uuid import UUID

import pytest
from sqlalchemy import Table

from app.modules.logging.models import AuditLog, UserActivity
from app.shared_kernel.infrastructure.database import Base

TABLES = {
    "user_activity": {
        "ix_user_activity_tenant_id_created_at",
        "ix_user_activity_telegram_user_id_created_at",
        "ix_user_activity_bot_id_created_at",
    },
    "audit_logs": {"ix_audit_logs_created_at", "ix_audit_logs_tenant_id_created_at"},
}


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


@pytest.mark.parametrize("name", sorted(TABLES))
def test_partitioned_table_shape(name: str) -> None:
    table = _table(name)
    assert table.dialect_options["postgresql"]["partition_by"] == "RANGE (created_at)"
    assert [column.name for column in table.primary_key.columns] == ["id", "created_at"]
    assert table.primary_key.name == f"pk_{name}"
    assert {index.name for index in table.indexes} == TABLES[name]
    assert "updated_at" not in table.c
    assert "is_deleted" not in table.c
    assert not table.foreign_keys
    for column in ("id", "created_at"):
        assert table.c[column].server_default is None
        assert table.c[column].default is not None
        assert not table.c[column].nullable


def test_python_defaults() -> None:
    id_default = _table("user_activity").c.id.default
    created_default = _table("user_activity").c.created_at.default
    assert id_default is not None and created_default is not None
    assert isinstance(id_default.arg(None), UUID)  # type: ignore[attr-defined]
    assert created_default.arg(None).tzinfo is UTC  # type: ignore[attr-defined]


def test_metadata_attribute_maps_to_metadata_column() -> None:
    assert UserActivity.metadata_.property.columns[0].name == "metadata"
    assert "metadata" in _table("user_activity").c


def test_tz_47_2_columns_and_nullability() -> None:
    table = _table("user_activity")
    for column in ("button", "callback_data", "screen_from", "screen_current", "screen_to"):
        assert table.c[column].nullable
    assert not table.c.bot_id.nullable
    assert not table.c.action_type.nullable


def test_audit_log_actor_columns() -> None:
    table = _table("audit_logs")
    assert table.c.actor_id.type.__class__.__name__ == "BigInteger"
    assert not table.c.actor_type.nullable
    assert table.c.bot_id.nullable
    checks = {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"}
    assert checks == {"ck_audit_logs_actor_type"}
    assert AuditLog.__tablename__ == "audit_logs"
