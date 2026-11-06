"""Tests for :mod:`app.shared_kernel.domain.exceptions`."""

from __future__ import annotations

import pytest

from app.shared_kernel.domain.exceptions import (
    ConfigurationError,
    DomainError,
    EntityAlreadyExistsError,
    EntityNotFoundError,
    PermissionDeniedError,
    StorageError,
    TenantIsolationError,
    ValidationError,
)

SUBCLASSES = (
    EntityNotFoundError,
    EntityAlreadyExistsError,
    ValidationError,
    PermissionDeniedError,
    TenantIsolationError,
    StorageError,
    ConfigurationError,
)


def test_domain_error_defaults() -> None:
    error = DomainError()
    assert error.message == ""
    assert error.code is None
    assert str(error) == ""


def test_domain_error_keeps_message_and_code() -> None:
    error = DomainError("boom", code="boom_code")
    assert error.message == "boom"
    assert error.code == "boom_code"
    assert str(error) == "boom"
    assert "boom_code" in repr(error)


@pytest.mark.parametrize("exc_type", SUBCLASSES)
def test_subclasses_inherit_from_domain_error(exc_type: type[DomainError]) -> None:
    error = exc_type("msg", code="c")
    assert isinstance(error, DomainError)
    assert isinstance(error, Exception)
    assert error.message == "msg"
    assert error.code == "c"
    assert str(error) == "msg"


def test_subclasses_are_catchable_as_domain_error() -> None:
    with pytest.raises(DomainError):
        raise EntityNotFoundError("missing", code="entity_not_found")


def test_tenant_isolation_error_is_not_a_permission_error() -> None:
    # Different failure semantics: isolation breach must be distinguishable
    # from a plain permission denial for audit logging.
    assert not issubclass(TenantIsolationError, PermissionDeniedError)
    assert issubclass(TenantIsolationError, DomainError)
