"""Tests for :mod:`app.core.migrations_support` (Alembic autogenerate filters, model importer)."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Column, Index, Integer, MetaData, Table

from app.core.migrations_support import (
    MANAGED_SCHEMAS,
    MODEL_MODULES,
    PARTITIONED_PARENTS,
    import_all_models,
    include_name,
    include_object,
)


@pytest.mark.parametrize("schema", ["partman", "archive", "information_schema"])
def test_include_name_rejects_unmanaged_schemas(schema: str) -> None:
    assert include_name(schema, "schema", {}) is False


@pytest.mark.parametrize("schema", [None, "public"])
def test_include_name_accepts_default_and_public(schema: str | None) -> None:
    assert schema in MANAGED_SCHEMAS
    assert include_name(schema, "schema", {}) is True


# pg_partman 5.5.0 naming: daily+ intervals -> _pYYYYMMDD, sub-daily -> _pYYYYMMDD_HH24MISS.
PARTITIONS = ("audit_logs_p20260101", "user_activity_p20260101_000000", "audit_logs_default")
NOT_PARTITIONS = (
    "foo_default",
    "foo_p20260101",
    "audit_logs_p2026",
    "audit_logs_p2026_01",
    "audit_logs_p20260101_00",
    "audit_logs",
    "user_activity",
)


def test_partitioned_parents() -> None:
    assert frozenset({"user_activity", "audit_logs"}) == PARTITIONED_PARENTS


@pytest.mark.parametrize("name", PARTITIONS)
def test_include_name_skips_partitions_before_reflection(name: str) -> None:
    assert include_name(name, "table", {"schema_name": None}) is False


@pytest.mark.parametrize("name", NOT_PARTITIONS)
def test_include_name_keeps_other_tables(name: str) -> None:
    assert include_name(name, "table", {"schema_name": None}) is True


def _table(name: str, schema: str | None = None) -> Table:
    table = Table(name, MetaData(), Column("id", Integer, primary_key=True), schema=schema)
    Index(f"ix_{name}_id", table.c.id)
    return table


def _index(table: Table) -> Index:
    return next(iter(table.indexes))


@pytest.mark.parametrize("name", PARTITIONS)
def test_include_object_skips_reflected_partition(name: str) -> None:
    table = _table(name)
    assert include_object(table, name, "table", True, None) is False


@pytest.mark.parametrize("name", PARTITIONS)
def test_include_object_skips_partition_children(name: str) -> None:
    table = _table(name)
    assert include_object(table.c.id, "id", "column", True, None) is False
    index = _index(table)
    assert include_object(index, index.name, "index", True, None) is False


@pytest.mark.parametrize("name", NOT_PARTITIONS)
def test_include_object_keeps_reflected_non_partitions(name: str) -> None:
    # Includes the parents ``audit_logs`` / ``user_activity`` reflected with compare_to=None.
    assert include_object(_table(name), name, "table", True, None) is True


@pytest.mark.parametrize("name", sorted(PARTITIONED_PARENTS))
def test_include_object_keeps_parent_children(name: str) -> None:
    table = _table(name)
    assert include_object(table.c.id, "id", "column", True, None) is True
    index = _index(table)
    assert include_object(index, index.name, "index", True, None) is True


def test_include_object_keeps_metadata_tables() -> None:
    declared = _table("user_activity")
    assert include_object(declared, "user_activity", "table", False, None) is True
    # A partition-like name declared in metadata is still managed by autogenerate.
    reflected = _table("audit_logs_default")
    compare_to = _table("audit_logs_default")
    assert include_object(reflected, "audit_logs_default", "table", True, compare_to) is True


def test_include_object_skips_service_schemas() -> None:
    for schema in ("partman", "archive"):
        table = _table("part_config", schema=schema)
        assert include_object(table, "part_config", "table", True, None) is False
        assert include_object(table.c.id, "id", "column", True, None) is False


def test_model_modules_cover_all_business_modules() -> None:
    assert len(MODEL_MODULES) == 9
    assert all(name.startswith("app.modules.") for name in MODEL_MODULES)


@pytest.fixture()
def fake_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    broken = tmp_path / "tgshop_broken_pkg"
    broken.mkdir()
    (broken / "__init__.py").write_text("import tgshop_missing_dependency  # noqa\n")
    (broken / "models.py").write_text("")
    good = tmp_path / "tgshop_good_pkg"
    good.mkdir()
    (good / "__init__.py").write_text("")
    (good / "models.py").write_text("VALUE = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    for name in list(sys.modules):
        if name.startswith(("tgshop_broken_pkg", "tgshop_good_pkg")):
            del sys.modules[name]


@pytest.mark.usefixtures("fake_packages")
def test_import_all_models_propagates_broken_dependency() -> None:
    with pytest.raises(ModuleNotFoundError) as excinfo:
        import_all_models(["tgshop_broken_pkg.models"])
    assert excinfo.value.name == "tgshop_missing_dependency"


@pytest.mark.usefixtures("fake_packages")
def test_import_all_models_skips_missing_modules() -> None:
    imported = import_all_models(
        ["tgshop_absent_pkg.models", "tgshop_good_pkg.missing", "tgshop_good_pkg.models"]
    )
    assert imported == ["tgshop_good_pkg.models"]
