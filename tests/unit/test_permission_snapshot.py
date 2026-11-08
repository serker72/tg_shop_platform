"""Unit tests of :class:`PermissionSnapshot` (decision matrix and JSON round trip)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.modules.iam.permissions import (
    GLOBAL_PERMISSIONS,
    PRODUCTS_EDIT,
    PRODUCTS_VIEW,
    TENANTS_VIEW_ALL,
)
from app.modules.iam.snapshot import (
    SNAPSHOT_VERSION,
    Decision,
    DecisionSource,
    PermissionSnapshot,
)

SHOP = uuid4()
OTHER_SHOP = uuid4()


def snap(**kwargs: object) -> PermissionSnapshot:
    kwargs.setdefault("user_id", uuid4())
    kwargs.setdefault("tenants", frozenset({SHOP}))
    return PermissionSnapshot(**kwargs)  # type: ignore[arg-type]


def roles(*slugs: str, role: str = "moderator") -> dict[str, frozenset[str]]:
    return {slug: frozenset({role}) for slug in slugs}


def test_unknown_user_is_denied() -> None:
    missing = PermissionSnapshot.missing()
    assert not missing.exists
    assert missing.decide(PRODUCTS_VIEW) == Decision(False, DecisionSource.UNKNOWN_USER)
    assert missing.decide(PRODUCTS_VIEW, SHOP).source is DecisionSource.UNKNOWN_USER


def test_superuser_is_allowed_everywhere() -> None:
    s = snap(is_superuser=True, tenants=frozenset())
    assert s.decide(PRODUCTS_VIEW) == Decision(True, DecisionSource.SUPERUSER)
    assert s.decide(PRODUCTS_VIEW, OTHER_SHOP).source is DecisionSource.SUPERUSER


def test_role_grants_with_all_source_roles() -> None:
    s = snap(role_permissions={PRODUCTS_VIEW: frozenset({"moderator", "admin"})})
    decision = s.decide(PRODUCTS_VIEW, SHOP)
    assert decision.allowed
    assert decision.source is DecisionSource.ROLE
    assert decision.roles == frozenset({"moderator", "admin"})
    assert s.decide(PRODUCTS_EDIT, SHOP) == Decision(False, DecisionSource.NOT_GRANTED)


def test_global_allow() -> None:
    s = snap(global_allow=frozenset({PRODUCTS_VIEW}))
    assert s.decide(PRODUCTS_VIEW).source is DecisionSource.OVERRIDE_ALLOW_GLOBAL
    assert s.decide(PRODUCTS_VIEW, SHOP).source is DecisionSource.OVERRIDE_ALLOW_GLOBAL


def test_global_deny_beats_role() -> None:
    s = snap(role_permissions=roles(PRODUCTS_VIEW), global_deny=frozenset({PRODUCTS_VIEW}))
    assert s.decide(PRODUCTS_VIEW) == Decision(False, DecisionSource.OVERRIDE_DENY_GLOBAL)
    assert s.decide(PRODUCTS_VIEW, SHOP).source is DecisionSource.OVERRIDE_DENY_GLOBAL


def test_tenant_deny_beats_role_and_global_allow() -> None:
    s = snap(
        tenants=frozenset({SHOP, OTHER_SHOP}),
        role_permissions=roles(PRODUCTS_VIEW, PRODUCTS_EDIT),
        global_allow=frozenset({PRODUCTS_EDIT}),
        tenant_deny={SHOP: frozenset({PRODUCTS_VIEW, PRODUCTS_EDIT})},
    )
    for slug in (PRODUCTS_VIEW, PRODUCTS_EDIT):
        assert s.decide(slug, SHOP) == Decision(False, DecisionSource.OVERRIDE_DENY_TENANT)
        assert s.decide(slug, OTHER_SHOP).allowed


def test_tenant_allow_only_in_its_shop() -> None:
    s = snap(
        tenants=frozenset({SHOP, OTHER_SHOP}),
        tenant_allow={SHOP: frozenset({PRODUCTS_EDIT})},
    )
    assert s.decide(PRODUCTS_EDIT, SHOP).source is DecisionSource.OVERRIDE_ALLOW_TENANT
    assert s.decide(PRODUCTS_EDIT, OTHER_SHOP).source is DecisionSource.NOT_GRANTED


def test_no_tenant_access_even_with_role() -> None:
    s = snap(
        role_permissions=roles(PRODUCTS_VIEW), tenant_allow={OTHER_SHOP: frozenset({PRODUCTS_VIEW})}
    )
    assert s.decide(PRODUCTS_VIEW, OTHER_SHOP) == Decision(False, DecisionSource.NO_TENANT_ACCESS)
    assert s.decide(PRODUCTS_VIEW, SHOP).allowed


@pytest.mark.parametrize("via", ["role", "global_allow"])
def test_view_all_opens_every_shop(via: str) -> None:
    kwargs: dict[str, object] = {"tenants": frozenset(), "role_permissions": roles(PRODUCTS_VIEW)}
    if via == "role":
        kwargs["role_permissions"] = {**roles(PRODUCTS_VIEW), **roles(TENANTS_VIEW_ALL)}
    else:
        kwargs["global_allow"] = frozenset({TENANTS_VIEW_ALL})
    s = snap(**kwargs)
    assert s.decide(PRODUCTS_VIEW, OTHER_SHOP).source is DecisionSource.ROLE
    assert s.has_tenant_access(uuid4())


def test_global_deny_of_view_all_revokes_shop_access() -> None:
    s = snap(
        tenants=frozenset({SHOP}),
        role_permissions={**roles(PRODUCTS_VIEW), **roles(TENANTS_VIEW_ALL)},
        global_deny=frozenset({TENANTS_VIEW_ALL}),
    )
    assert s.decide(PRODUCTS_VIEW, OTHER_SHOP).source is DecisionSource.NO_TENANT_ACCESS
    assert s.decide(PRODUCTS_VIEW, SHOP).allowed
    assert s.decide(TENANTS_VIEW_ALL).source is DecisionSource.OVERRIDE_DENY_GLOBAL


def test_tenant_deny_of_view_all_does_not_revoke_global_access() -> None:
    s = snap(
        tenants=frozenset(),
        role_permissions={**roles(PRODUCTS_VIEW), **roles(TENANTS_VIEW_ALL)},
        tenant_deny={SHOP: frozenset({TENANTS_VIEW_ALL})},
    )
    assert s.decide(PRODUCTS_VIEW, SHOP).allowed


def test_without_tenant_tenant_overrides_are_ignored() -> None:
    s = snap(
        role_permissions=roles(PRODUCTS_VIEW),
        tenant_deny={SHOP: frozenset({PRODUCTS_VIEW})},
        tenant_allow={SHOP: frozenset({PRODUCTS_EDIT})},
    )
    assert s.decide(PRODUCTS_VIEW).source is DecisionSource.ROLE
    assert s.decide(PRODUCTS_EDIT).source is DecisionSource.NOT_GRANTED


def test_view_all_check_without_tenant_is_a_plain_global_check() -> None:
    assert snap(role_permissions=roles(TENANTS_VIEW_ALL)).decide(TENANTS_VIEW_ALL).allowed
    assert not snap().decide(TENANTS_VIEW_ALL).allowed


@pytest.mark.parametrize("slug", sorted(GLOBAL_PERMISSIONS))
def test_tenant_overrides_of_global_permissions_are_ignored(slug: str) -> None:
    s = snap(
        tenants=frozenset({SHOP}),
        role_permissions=roles(slug),
        tenant_deny={SHOP: frozenset({slug})},
        tenant_allow={SHOP: frozenset({slug})},
    )
    assert s.decide(slug, SHOP).source is DecisionSource.ROLE
    bare = snap(tenants=frozenset({SHOP}), tenant_allow={SHOP: frozenset({slug})})
    assert bare.decide(slug, SHOP).source is DecisionSource.NOT_GRANTED


def test_generation_stamps_survive_round_trip() -> None:
    original = snap().with_generations(user_generation=7, global_generation=11)
    assert original.generation == 7
    assert PermissionSnapshot.from_json(original.to_json()) == original


def test_json_round_trip() -> None:
    original = PermissionSnapshot(
        user_id=uuid4(),
        role_permissions={PRODUCTS_VIEW: frozenset({"admin", "moderator"})},
        global_allow=frozenset({PRODUCTS_EDIT}),
        global_deny=frozenset({TENANTS_VIEW_ALL}),
        tenant_allow={SHOP: frozenset({"orders.view"})},
        tenant_deny={OTHER_SHOP: frozenset({"orders.manage"})},
        tenants=frozenset({SHOP, OTHER_SHOP}),
        next_expiry=datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    raw = original.to_json()
    assert json.loads(raw)["v"] == SNAPSHOT_VERSION
    assert PermissionSnapshot.from_json(raw) == original
    for value in (PermissionSnapshot.missing(), snap(is_superuser=True)):
        assert PermissionSnapshot.from_json(value.to_json()) == value


def _payload(**overrides: object) -> bytes:
    data = json.loads(PermissionSnapshot.missing().to_json())
    data.update(overrides)
    return json.dumps(data).encode()


@pytest.mark.parametrize(
    "overrides",
    [
        {"su": 1},
        {"su": "true"},
        {"rp": {"products.view": "moderator"}},
        {"rp": {"products.view": [1]}},
        {"rp": [["products.view"]]},
        {"ga": {"products.view": 1}},
        {"gd": "products.view"},
        {"ta": {"not-a-uuid": []}},
        {"ta": {"f47ac10b-58cc-4372-a567-0e02b2c3d479": "x"}},
        {"td": []},
        {"t": [1]},
        {"ne": 123},
        {"ne": "yesterday"},
        {"g": True},
        {"g": "1"},
        {"gg": 1.5},
        {"user_id": 5},
    ],
)
def test_strict_decode_rejects_wrong_types(overrides: dict[str, object]) -> None:
    assert PermissionSnapshot.from_json(_payload(**overrides)) is None


def test_decode_rejects_non_str_role_permission_keys() -> None:
    data = json.loads(PermissionSnapshot.missing().to_json())
    data["rp"] = {1: []}  # JSON turns the int key into "1": indistinguishable from str
    assert PermissionSnapshot.from_json(json.dumps(data)) is not None
    data["rp"] = {1: ["moderator"]}
    decoded = PermissionSnapshot.from_json(json.dumps(data))
    assert decoded is not None  # str key after JSON round trip, roles intact
    assert decoded.role_permissions == {"1": frozenset({"moderator"})}


def test_decode_rejects_missing_and_unknown_fields() -> None:
    data = json.loads(PermissionSnapshot.missing().to_json())
    for name in data:
        without = {key: value for key, value in data.items() if key != name}
        assert PermissionSnapshot.from_json(json.dumps(without).encode()) is None, name
    assert PermissionSnapshot.from_json(_payload(extra=1)) is None
    assert PermissionSnapshot.from_json(_payload(note="x")) is None


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b"{}",
        json.dumps({"v": 2}).encode(),
        json.dumps({**json.loads(PermissionSnapshot.missing().to_json()), "v": 999}).encode(),
        json.dumps({**json.loads(PermissionSnapshot.missing().to_json()), "user_id": "x"}).encode(),
        json.dumps({**json.loads(PermissionSnapshot.missing().to_json()), "t": 5}).encode(),
    ],
)
def test_broken_or_foreign_payload_is_none(raw: bytes) -> None:
    assert PermissionSnapshot.from_json(raw) is None


def test_snapshot_is_immutable() -> None:
    s = snap()
    with pytest.raises(AttributeError):
        s.is_superuser = True  # type: ignore[misc]
    assert isinstance(s.user_id, UUID)
