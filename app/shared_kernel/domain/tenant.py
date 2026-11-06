"""Explicit tenant context of the multi-tenant platform.

Tenant isolation is enforced at the Service / Repository level (TZ, lines 111-124): every
service and repository that touches tenant data receives a :class:`TenantScope` explicitly
and filters / checks by ``tenant_id``. Neither a ``contextvar`` nor PostgreSQL RLS is used
(user decision); RLS may be added later as an extra defence layer, not as a replacement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from app.shared_kernel.domain.exceptions import TenantIsolationError

__all__ = ["TenantScope"]


@dataclass(frozen=True, slots=True)
class TenantScope:
    """Immutable tenant context passed explicitly into services and repositories.

    Raises:
        TenantIsolationError: ``code="tenant_id_invalid"`` when ``tenant_id`` is not a
            :class:`uuid.UUID` (strings are rejected too, so a typo cannot widen access).
    """

    tenant_id: UUID

    def __post_init__(self) -> None:
        # ``cast`` hides the declared type: the check guards untyped callers at runtime.
        if not isinstance(cast(object, self.tenant_id), UUID):
            raise TenantIsolationError("tenant_id must be a UUID", code="tenant_id_invalid")

    def ensure_owns(self, entity_tenant_id: UUID) -> None:
        """Fail unless the entity belongs to this tenant.

        The message never contains either id, so a foreign tenant id cannot leak through
        logs or API error bodies.

        Raises:
            TenantIsolationError: ``code="tenant_mismatch"`` on a foreign entity.
        """
        if entity_tenant_id != self.tenant_id:
            raise TenantIsolationError("tenant mismatch", code="tenant_mismatch")
