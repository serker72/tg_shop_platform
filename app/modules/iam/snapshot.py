"""Permission snapshot of one user and the pure decision rule.

The Redis cache stores the *raw* data of a user (role permissions, overrides, shops), not
the final answers: every check is evaluated in memory by :meth:`PermissionSnapshot.decide`,
so one cache entry serves every ``(permission, tenant)`` pair.

``generation`` / ``global_generation`` are cache bookkeeping (see
:mod:`app.modules.iam.cache`); they are stamped by the cache and ignored by ``decide``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.modules.iam.permissions import GLOBAL_PERMISSIONS, TENANTS_VIEW_ALL

__all__ = ["SNAPSHOT_VERSION", "Decision", "DecisionSource", "PermissionSnapshot"]

#: Version of the JSON layout (``"v"``); any other value is treated as a cache miss.
SNAPSHOT_VERSION = 1

_EMPTY: frozenset[str] = frozenset()

#: Exact set of :meth:`PermissionSnapshot.to_json` fields; anything else is a broken payload.
_PAYLOAD_KEYS = frozenset(
    {"v", "user_id", "su", "rp", "ga", "gd", "ta", "td", "t", "ne", "g", "gg"}
)


class DecisionSource(StrEnum):
    """Why :meth:`PermissionSnapshot.decide` allowed or denied a permission."""

    SUPERUSER = "superuser"
    ROLE = "role"
    OVERRIDE_ALLOW_GLOBAL = "override_allow_global"
    OVERRIDE_ALLOW_TENANT = "override_allow_tenant"
    OVERRIDE_DENY_GLOBAL = "override_deny_global"
    OVERRIDE_DENY_TENANT = "override_deny_tenant"
    NO_TENANT_ACCESS = "no_tenant_access"
    NOT_GRANTED = "not_granted"
    UNKNOWN_USER = "unknown_user"


@dataclass(frozen=True, slots=True)
class Decision:
    """Result of a check; ``roles`` lists the source roles when ``source`` is ``ROLE``."""

    allowed: bool
    source: DecisionSource
    roles: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PermissionSnapshot:
    """Live permission data of one user at load time.

    Attributes:
        user_id: ``users.id``; ``None`` - unknown, inactive or deleted user.
        is_superuser: bypasses every check (other fields are not loaded then).
        role_permissions: permission slug -> slugs of the live roles granting it.
        global_allow / global_deny: overrides with ``tenant_id IS NULL``.
        tenant_allow / tenant_deny: shop id -> overridden slugs of that shop.
        tenants: shops assigned in ``tenant_moderators``.
        next_expiry: earliest ``expires_at`` among the loaded overrides; the cache TTL
            never outlives it.
        generation / global_generation: cache generation stamps (not part of the decision).

    Existing shops: the snapshot carries only the ids the user moderates; whether a shop
    passed as ``tenant_id`` still exists is the *caller's* concern (a service acting on a
    shop loads it first). A soft-deleted shop loses its ``tenant_moderators`` rows from the
    snapshot, so ``decide`` answers ``no_tenant_access`` for it.
    """

    user_id: UUID | None
    is_superuser: bool = False
    role_permissions: Mapping[str, frozenset[str]] = field(default_factory=dict)
    global_allow: frozenset[str] = frozenset()
    global_deny: frozenset[str] = frozenset()
    tenant_allow: Mapping[UUID, frozenset[str]] = field(default_factory=dict)
    tenant_deny: Mapping[UUID, frozenset[str]] = field(default_factory=dict)
    tenants: frozenset[UUID] = frozenset()
    next_expiry: datetime | None = None
    generation: int = 0
    global_generation: int = 0

    @classmethod
    def missing(cls) -> PermissionSnapshot:
        """Snapshot of an unknown / inactive / deleted user: every check is denied."""
        return cls(user_id=None)

    @property
    def exists(self) -> bool:
        """``True`` for a live user."""
        return self.user_id is not None

    def with_generations(
        self, *, user_generation: int, global_generation: int
    ) -> PermissionSnapshot:
        """Copy stamped with the cache generations (used right before writing to Redis)."""
        return replace(self, generation=user_generation, global_generation=global_generation)

    # --- decision ---------------------------------------------------------------------

    def has_tenant_access(self, tenant_id: UUID) -> bool:
        """Shop assigned in ``tenant_moderators`` or ``tenants.view_all`` held globally."""
        return tenant_id in self.tenants or self._holds_globally(TENANTS_VIEW_ALL)

    def _holds_globally(self, slug: str) -> bool:
        if slug in self.global_deny:
            return False
        return slug in self.role_permissions or slug in self.global_allow

    def decide(self, slug: str, tenant_id: UUID | None = None) -> Decision:
        """Evaluate ``slug`` (optionally inside shop ``tenant_id``).

        Order: unknown user -> superuser -> shop access -> any applicable deny -> roles ->
        global allow -> tenant allow -> not granted. With ``tenant_id=None`` only roles and
        global overrides count; tenant overrides are ignored.

        Tenant-scoped overrides of :data:`GLOBAL_PERMISSIONS` are rejected by the write
        service; the resolver ignores them as well (defense in depth): such a row can only
        come from a bug or a manual database edit and must never narrow a global permission.
        """
        if self.user_id is None:
            return Decision(allowed=False, source=DecisionSource.UNKNOWN_USER)
        if self.is_superuser:
            return Decision(allowed=True, source=DecisionSource.SUPERUSER)
        if tenant_id is not None and not self.has_tenant_access(tenant_id):
            return Decision(allowed=False, source=DecisionSource.NO_TENANT_ACCESS)
        shop = tenant_id if tenant_id is not None and slug not in GLOBAL_PERMISSIONS else None
        if slug in self.global_deny:
            return Decision(allowed=False, source=DecisionSource.OVERRIDE_DENY_GLOBAL)
        if shop is not None and slug in self.tenant_deny.get(shop, _EMPTY):
            return Decision(allowed=False, source=DecisionSource.OVERRIDE_DENY_TENANT)
        roles = self.role_permissions.get(slug, _EMPTY)
        if roles:
            return Decision(allowed=True, source=DecisionSource.ROLE, roles=roles)
        if slug in self.global_allow:
            return Decision(allowed=True, source=DecisionSource.OVERRIDE_ALLOW_GLOBAL)
        if shop is not None and slug in self.tenant_allow.get(shop, _EMPTY):
            return Decision(allowed=True, source=DecisionSource.OVERRIDE_ALLOW_TENANT)
        return Decision(allowed=False, source=DecisionSource.NOT_GRANTED)

    # --- serialisation ----------------------------------------------------------------

    def to_json(self) -> bytes:
        """Compact JSON with ``"v": SNAPSHOT_VERSION`` (sorted lists - stable output)."""

        def by_tenant(data: Mapping[UUID, frozenset[str]]) -> dict[str, list[str]]:
            return {str(tid): sorted(slugs) for tid, slugs in data.items()}

        payload: dict[str, Any] = {
            "v": SNAPSHOT_VERSION,
            "user_id": None if self.user_id is None else str(self.user_id),
            "su": self.is_superuser,
            "rp": {slug: sorted(roles) for slug, roles in self.role_permissions.items()},
            "ga": sorted(self.global_allow),
            "gd": sorted(self.global_deny),
            "ta": by_tenant(self.tenant_allow),
            "td": by_tenant(self.tenant_deny),
            "t": sorted(str(tid) for tid in self.tenants),
            "ne": None if self.next_expiry is None else self.next_expiry.isoformat(),
            "g": self.generation,
            "gg": self.global_generation,
        }
        return json.dumps(payload, separators=(",", ":")).encode()

    @classmethod
    def from_json(cls, raw: bytes | str) -> PermissionSnapshot | None:
        """Strictly decode :meth:`to_json` output; ``None`` (fail-closed) on any mismatch.

        Every field is type-checked: a bool must be a real ``bool`` (not ``0``/``1``), a
        list must be a list of ``str``, tenant ids must be ``UUID``s, ``next_expiry`` must
        be an ISO datetime. The key set must match :data:`_PAYLOAD_KEYS` exactly (missing
        and unknown fields are rejected), ``rp`` keys must be ``str``. A broken or foreign
        payload is a cache miss.
        """
        try:
            return cls._decode(raw)
        except (ValueError, TypeError, KeyError, AttributeError):
            return None

    @classmethod
    def _decode(cls, raw: bytes | str) -> PermissionSnapshot:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("v") != SNAPSHOT_VERSION:
            raise ValueError("unknown snapshot version")

        def as_bool(value: Any, name: str) -> bool:
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be a bool")
            return value

        def as_int(value: Any, name: str) -> int:
            if type(value) is not int:  # noqa: E721 - bool is an int subclass
                raise TypeError(f"{name} must be an int")
            return value

        def as_str_list(value: Any, name: str) -> frozenset[str]:
            if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
                raise TypeError(f"{name} must be a list of str")
            return frozenset(value)

        def as_by_tenant(value: Any, name: str) -> dict[UUID, frozenset[str]]:
            if not isinstance(value, dict):
                raise TypeError(f"{name} must be a dict")
            result: dict[UUID, frozenset[str]] = {}
            for tid, slugs in value.items():
                if not isinstance(tid, str):
                    raise TypeError(f"{name} keys must be str")
                result[UUID(tid)] = as_str_list(slugs, name)
            return result

        if set(data) != _PAYLOAD_KEYS:
            raise ValueError(f"unexpected snapshot fields: {sorted(set(data) ^ _PAYLOAD_KEYS)}")
        role_permissions_raw = data["rp"]
        if not isinstance(role_permissions_raw, dict):
            raise TypeError("rp must be a dict")
        role_permissions: dict[str, frozenset[str]] = {}
        for slug, roles in role_permissions_raw.items():
            if not isinstance(slug, str):
                raise TypeError("rp keys must be str")
            role_permissions[slug] = as_str_list(roles, "rp")

        user_id = data["user_id"]
        if user_id is not None and not isinstance(user_id, str):
            raise TypeError("user_id must be a str or None")
        next_expiry = data["ne"]
        if next_expiry is not None and not isinstance(next_expiry, str):
            raise TypeError("ne must be a str or None")
        return cls(
            user_id=None if user_id is None else UUID(user_id),
            is_superuser=as_bool(data["su"], "su"),
            role_permissions=role_permissions,
            global_allow=as_str_list(data["ga"], "ga"),
            global_deny=as_str_list(data["gd"], "gd"),
            tenant_allow=as_by_tenant(data["ta"], "ta"),
            tenant_deny=as_by_tenant(data["td"], "td"),
            tenants=frozenset(UUID(tid) for tid in as_str_list(data["t"], "t")),
            next_expiry=None if next_expiry is None else datetime.fromisoformat(next_expiry),
            generation=as_int(data["g"], "g"),
            global_generation=as_int(data["gg"], "gg"),
        )
