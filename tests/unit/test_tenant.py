from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import cast
from uuid import UUID, uuid4

import pytest

from app.shared_kernel import TenantScope as ReexportedScope
from app.shared_kernel.domain import TenantIsolationError
from app.shared_kernel.domain.tenant import TenantScope


def test_reexported_from_shared_kernel() -> None:
    assert ReexportedScope is TenantScope


def test_scope_is_frozen() -> None:
    scope = TenantScope(uuid4())
    with pytest.raises(FrozenInstanceError):
        # Assigning to a frozen dataclass field is exactly what is being tested.
        scope.tenant_id = uuid4()  # type: ignore[misc]


@pytest.mark.parametrize("value", ["not-a-uuid", str(uuid4()), 42, None])
def test_invalid_tenant_id_rejected(value: object) -> None:
    with pytest.raises(TenantIsolationError) as info:
        TenantScope(cast(UUID, value))
    assert info.value.code == "tenant_id_invalid"


def test_ensure_owns_same_tenant(tenant: UUID, tenant_scope: TenantScope) -> None:
    tenant_scope.ensure_owns(tenant)


def test_ensure_owns_foreign_tenant_without_leaking_id(
    tenant_scope: TenantScope, other_tenant_scope: TenantScope
) -> None:
    foreign = other_tenant_scope.tenant_id
    with pytest.raises(TenantIsolationError) as info:
        tenant_scope.ensure_owns(foreign)
    assert info.value.code == "tenant_mismatch"
    assert info.value.message == "tenant mismatch"
    assert str(foreign) not in str(info.value)
    assert str(tenant_scope.tenant_id) not in str(info.value)
