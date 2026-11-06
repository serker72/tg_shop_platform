"""Tests for the entity mixins in :mod:`app.shared_kernel.domain.base_entity`.

Probes are declared in the recommended project style::

    class Probe(BusinessEntity, Base):
        __tablename__ = "probe"

``BaseEntity`` is a mixin (no ``__tablename__``). Listing it before the
declarative base is a style convention; ``(Base, BusinessEntity)`` maps the same
columns as well (see ``test_reverse_base_order_maps_identically``).

The probes live on a private ``DeclarativeBase`` with their own ``MetaData``
(but the production naming convention), so they never leak into
``Base.metadata``, which Alembic autogenerate will read later.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

import pytest
from sqlalchemy import (
    Boolean,
    DefaultClause,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    inspect,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, registry

from app.core.migrations_support import import_all_models
from app.shared_kernel.domain.base_entity import (
    TIMESTAMP_MAPPER_ARGS,
    BaseEntity,
    BusinessEntity,
    SoftDeleteMixin,
    TimestampMixin,
)
from app.shared_kernel.infrastructure.database import (
    NAMING_CONVENTION,
    Base,
)
from app.shared_kernel.infrastructure.database import (
    metadata as shared_metadata,
)


class _TestBase(DeclarativeBase):
    """Isolated declarative root sharing the production naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class _BusinessProbe(BusinessEntity, _TestBase):
    __tablename__ = "probe_business"

    name: Mapped[str] = mapped_column(String(32))


class _SoftDeleteProbe(SoftDeleteMixin, _TestBase):
    __tablename__ = "probe_soft_delete"

    id: Mapped[int] = mapped_column(primary_key=True)


class _ReverseOrderProbe(_TestBase, BusinessEntity):
    __tablename__ = "probe_reverse"


class _ExtendedArgsProbe(BusinessEntity, _TestBase):
    __tablename__ = "probe_extended_args"
    __mapper_args__: dict[str, Any] = {  # noqa: RUF012 - declarative directive
        **TIMESTAMP_MAPPER_ARGS,
        "confirm_deleted_rows": False,
    }


def _server_default_text(table: Table, column_name: str) -> str:
    default = table.c[column_name].server_default
    assert isinstance(default, DefaultClause)
    return str(default.arg)


def test_business_entity_tables_map_without_extra_bases() -> None:
    table = cast("Table", _BusinessProbe.__table__)

    assert table.name == "probe_business"
    assert set(table.columns.keys()) == {
        "id",
        "created_at",
        "updated_at",
        "is_active",
        "is_deleted",
        "name",
    }
    assert _TestBase.metadata.tables["probe_business"] is table


def test_mixins_are_not_mapped_classes() -> None:
    # Mixins are only usable as bases of a mapped class.
    for mixin in (BaseEntity, TimestampMixin, SoftDeleteMixin):
        assert not hasattr(mixin, "__table__")


def test_id_is_uuid_primary_key_with_python_default() -> None:
    column = _BusinessProbe.__table__.c.id
    assert column.primary_key
    assert column.type.python_type is uuid.UUID
    assert column.default is not None


def test_timestamps_are_tz_aware_with_server_defaults() -> None:
    table = cast("Table", _BusinessProbe.__table__)
    for name in ("created_at", "updated_at"):
        column = table.c[name]
        assert column.type.python_type is datetime
        assert getattr(column.type, "timezone", None) is True  # noqa: B009 - DateTime attr
        assert column.server_default is not None
        assert column.nullable is False
        assert "now()" in _server_default_text(table, name)

    assert table.c.updated_at.onupdate is not None
    assert table.c.created_at.onupdate is None


def test_soft_delete_flags_have_server_defaults() -> None:
    table = cast("Table", _BusinessProbe.__table__)
    for name in ("is_active", "is_deleted"):
        column = table.c[name]
        assert isinstance(column.type, Boolean)
        assert column.server_default is not None
        assert column.nullable is False

    assert _server_default_text(table, "is_active") == "true"
    assert _server_default_text(table, "is_deleted") == "false"


def test_is_deleted_column_is_indexed_with_convention() -> None:
    index_names = {index.name for index in cast("Table", _BusinessProbe.__table__).indexes}
    assert index_names == {"ix_probe_business_is_deleted"}
    assert NAMING_CONVENTION["ix"] == "ix_%(table_name)s_%(column_0_N_name)s"


def test_soft_delete_mixin_is_reusable_standalone() -> None:
    table = cast("Table", _SoftDeleteProbe.__table__)
    assert set(table.columns.keys()) == {"id", "is_active", "is_deleted"}
    assert {index.name for index in table.indexes} == {"ix_probe_soft_delete_is_deleted"}


def assert_timestamp_mappers_eager(reg: registry) -> int:
    """Fail if any ``TimestampMixin`` mapper in ``reg`` lost ``eager_defaults=True``.

    Returns the number of checked mappers (0 is valid: no business models yet).
    """
    offenders: list[str] = []
    checked = 0
    for mapper in reg.mappers:
        if not issubclass(mapper.class_, TimestampMixin):
            continue
        checked += 1
        if mapper.eager_defaults is not True:
            offenders.append(mapper.class_.__name__)
    assert not offenders, (
        f"eager_defaults is not True for {offenders}: merge TIMESTAMP_MAPPER_ARGS into "
        "__mapper_args__"
    )
    return checked


def test_project_timestamp_mappers_are_eager() -> None:
    import_all_models()
    assert assert_timestamp_mappers_eager(Base.registry) >= 0


def test_guard_passes_on_local_registry() -> None:
    assert assert_timestamp_mappers_eager(_TestBase.registry) >= 3


def test_guard_fails_when_mapper_args_override_drops_eager_defaults() -> None:
    class _BadBase(DeclarativeBase):
        metadata = MetaData(naming_convention=NAMING_CONVENTION)

    class _BadProbe(BusinessEntity, _BadBase):
        __tablename__ = "probe_bad_args"

        version: Mapped[int] = mapped_column()
        __mapper_args__: dict[str, Any] = {"version_id_col": version}  # noqa: RUF012

    with pytest.raises(AssertionError, match="_BadProbe"):
        assert_timestamp_mappers_eager(_BadBase.registry)


def test_timestamp_entities_use_eager_defaults() -> None:
    assert _BusinessProbe.__mapper__.eager_defaults is True


def test_subclass_can_extend_mapper_args() -> None:
    mapper = _ExtendedArgsProbe.__mapper__
    assert mapper.eager_defaults is True
    assert mapper.confirm_deleted_rows is False


def test_reverse_base_order_maps_identically() -> None:
    reverse = cast("Table", _ReverseOrderProbe.__table__)
    assert set(reverse.columns.keys()) == {
        "id",
        "created_at",
        "updated_at",
        "is_active",
        "is_deleted",
    }
    assert _ReverseOrderProbe.__mapper__.eager_defaults is True


def test_orm_update_returns_updated_at_without_lazy_load() -> None:
    # SQLite supports UPDATE ... RETURNING, so the real unit-of-work SQL is checked
    # without PostgreSQL. With eager_defaults="auto" no RETURNING would be emitted and
    # updated_at would stay expired (lazy IO -> MissingGreenlet under AsyncSession).
    engine = create_engine("sqlite://")
    _TestBase.metadata.create_all(engine, tables=[cast("Table", _BusinessProbe.__table__)])
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:  # noqa: ANN401
        statements.append(statement)

    with Session(engine) as session:
        probe = _BusinessProbe(name="a")
        session.add(probe)
        session.flush()
        probe.name = "b"
        statements.clear()
        session.flush()

        updates = [s for s in statements if s.lstrip().upper().startswith("UPDATE")]
        assert len(updates) == 1
        assert "RETURNING" in updates[0].upper()
        assert "updated_at" in updates[0].upper().split("RETURNING", 1)[1].lower()
        assert "updated_at" in updates[0].split("SET", 1)[1]
        assert "updated_at" not in inspect(probe).expired_attributes
    engine.dispose()


def test_probes_do_not_leak_into_shared_metadata() -> None:
    # Alembic autogenerates migrations from Base.metadata: test probes must not
    # appear there.
    assert "probe_business" not in shared_metadata.tables
    assert "probe_soft_delete" not in shared_metadata.tables
    assert Base.metadata is shared_metadata
