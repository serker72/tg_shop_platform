"""Tests for :mod:`app.shared_kernel.infrastructure.database`.

Constraint names are inspected on a private :class:`MetaData` instance so the
module-level ``metadata`` stays clean for Alembic autogenerate. No database
connection is used here: ``Base.metadata.create_all(dialect=...)`` produces the
DDL string that PostgreSQL would receive.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import CreateIndex, CreateTable

from app.shared_kernel.infrastructure.database import NAMING_CONVENTION, Base, metadata

EXPECTED_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class _ConventionBase(DeclarativeBase):
    """Isolated declarative root sharing the production naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class _Tenant(_ConventionBase):
    __tablename__ = "tenant"

    id: Mapped[int] = mapped_column(primary_key=True)


class _Product(_ConventionBase):
    __tablename__ = "product"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sku"),
        CheckConstraint("price >= 0", name="price_non_negative"),
        # The FK must be declared as a table-level constraint: an inline
        # ``ForeignKey("tenant.id")`` keeps ``ForeignKey.name`` as ``None`` in
        # SQLAlchemy 2.1, while the convention-based name lands on the wrapping
        # ``ForeignKeyConstraint`` and is used in DDL.
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    sku: Mapped[str] = mapped_column(String(32))
    price: Mapped[int] = mapped_column(Integer)


PRODUCT_TABLE: Table = _Product.__dict__["__table__"]
TENANT_TABLE: Table = _Tenant.__dict__["__table__"]


def _compile_ddl(construct: Any) -> str:  # noqa: ANN401 - DDL construct, no common protocol
    return str(construct.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


def _ddl() -> str:
    create_table = _compile_ddl(CreateTable(PRODUCT_TABLE))
    create_index = _compile_ddl(CreateIndex(next(iter(PRODUCT_TABLE.indexes))))
    return create_table + create_index


def test_convention_values_and_metadata_exported() -> None:
    assert NAMING_CONVENTION == EXPECTED_CONVENTION
    assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}
    assert Base.metadata is metadata
    assert metadata.naming_convention == NAMING_CONVENTION


def test_generated_ddl_contains_all_conventions() -> None:
    ddl = _ddl()

    assert "CONSTRAINT pk_product PRIMARY KEY (id)" in ddl
    assert "CONSTRAINT uq_product_tenant_id_sku UNIQUE (tenant_id, sku)" in ddl
    assert "CONSTRAINT ck_product_price_non_negative CHECK (price >= 0)" in ddl
    assert (
        "CONSTRAINT fk_product_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id)"
        in ddl
    )
    assert "CREATE INDEX ix_product_tenant_id ON product (tenant_id)" in ddl


def test_primary_key_of_referenced_table_follows_convention() -> None:
    ddl = _compile_ddl(CreateTable(TENANT_TABLE))
    assert "CONSTRAINT pk_tenant PRIMARY KEY (id)" in ddl


def test_generated_names_fit_postgres_identifier_limit() -> None:
    names = [
        token.split()[1]
        for token in _ddl().replace("(", " ").replace(")", " ").split("CONSTRAINT ")[1:]
    ]
    names = [name for name in names if not name.startswith("ON")]

    assert names
    assert all(len(name) <= 63 for name in names)
