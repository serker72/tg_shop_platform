"""IAM admin services: audit of every mutation, error codes and review regressions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam import admin_service as svc
from app.modules.iam.cache import GLOBAL_GENERATION_KEY
from app.modules.iam.models import (
    Permission,
    Role,
    RolePermission,
    Tenant,
    TenantSettings,
    User,
    UserPermissionOverride,
)
from app.modules.iam.permissions import (
    ORDERS_VIEW,
    PRODUCTS_VIEW,
    ROLE_ADMIN,
    ROLE_MODERATOR,
    ROLES_MANAGE,
)
from app.modules.iam.service import PermissionService
from app.modules.logging.models import AuditLog
from app.shared_kernel.domain.exceptions import DomainError, PermissionDeniedError
from tests.integration.test_iam_admin_service import (
    assert_consistent,
    make_user,
    perms,  # noqa: F401 - fixture
    services,  # noqa: F401 - fixture
    tg_id,
)

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.2")]

ROLE_SLUG = "audit_viewer"


@dataclass
class Ctx:
    s: dict[str, Any]
    admin: int
    target: int
    tenant: UUID
    role: UUID


async def _permission_id(session: AsyncSession, slug: str) -> UUID:
    return (await session.scalars(select(Permission.id).where(Permission.slug == slug))).one()


async def direct_override(
    session: AsyncSession, user_id: UUID, slug: str, *, granted: bool
) -> None:
    session.add(
        UserPermissionOverride(
            user_id=user_id, permission_id=await _permission_id(session, slug), is_granted=granted
        )
    )
    await session.commit()


@pytest.fixture
async def ctx(session: AsyncSession, services: dict[str, Any]) -> Ctx:  # noqa: F811
    admin = await make_user(session, role=ROLE_ADMIN)
    target = await make_user(session)
    tenant = Tenant(name="Shop", slug="audit-shop")
    session.add(tenant)
    await session.flush()
    session.add(TenantSettings(tenant_id=tenant.id))
    role = Role(name="Viewer", slug=ROLE_SLUG)
    session.add(role)
    await session.flush()
    session.add(
        RolePermission(role_id=role.id, permission_id=await _permission_id(session, PRODUCTS_VIEW))
    )
    await session.commit()
    return Ctx(services, admin.telegram_user_id, target.telegram_user_id, tenant.id, role.id)


Step = Callable[[Ctx], Awaitable[Any]]


async def _seq(c: Ctx, *steps: Step) -> None:
    for step in steps:
        await step(c)


def _t(target: str, *args: Any, **kwargs: Any) -> Step:
    service, method = target.split(".")
    return lambda c: getattr(c.s[service], method)(
        c.admin, *[a(c) if callable(a) else a for a in args], **kwargs
    )


TENANT = lambda c: c.tenant  # noqa: E731
TARGET = lambda c: c.target  # noqa: E731
ROLE = lambda c: c.role  # noqa: E731

#: (action, steps, expected before subset | None, expected after subset | None)
SCENARIOS: list[tuple[str, list[Step], dict[str, Any] | None, dict[str, Any] | None]] = [
    (
        "iam.tenant.update",
        [_t("tenant.update", TENANT, name="New")],
        {"name": "Shop"},
        {"name": "New"},
    ),
    (
        "iam.tenant.deactivate",
        [_t("tenant.deactivate", TENANT)],
        {"is_active": True},
        {"is_active": False},
    ),
    (
        "iam.tenant.activate",
        [_t("tenant.deactivate", TENANT), _t("tenant.activate", TENANT)],
        {"is_active": False},
        {"is_active": True},
    ),
    (
        "iam.tenant.delete",
        [_t("tenant.soft_delete", TENANT)],
        {"is_deleted": False},
        {"is_deleted": True},
    ),
    (
        "iam.tenant.restore",
        [_t("tenant.soft_delete", TENANT), _t("tenant.restore", TENANT)],
        {"is_deleted": True},
        {"is_deleted": False},
    ),
    (
        "iam.tenant.settings_update",
        [_t("tenant.update_settings", TENANT, {"is_store_open": True})],
        {"is_store_open": False},
        {"is_store_open": True},
    ),
    (
        "iam.tenant.create",
        [lambda c: c.s["tenant"].create(c.admin, name="Other", slug="audit-other")],
        None,
        {"slug": "audit-other", "is_active": True},
    ),
    (
        "iam.user.create",
        [lambda c: c.s["user"].add_by_telegram_id(c.admin, tg_id(), "@fresh")],
        None,
        {"telegram_user_name": "fresh", "is_active": True},
    ),
    (
        "iam.user.deactivate",
        [_t("user.deactivate", TARGET)],
        {"is_active": True},
        {"is_active": False},
    ),
    (
        "iam.user.activate",
        [_t("user.deactivate", TARGET), _t("user.activate", TARGET)],
        {"is_active": False},
        {"is_active": True},
    ),
    (
        "iam.user.delete",
        [_t("user.soft_delete", TARGET)],
        {"is_deleted": False},
        {"is_deleted": True},
    ),
    (
        "iam.user.restore",
        [_t("user.soft_delete", TARGET), _t("user.restore", TARGET)],
        {"is_deleted": True},
        {"is_deleted": False},
    ),
    (
        "iam.role.create",
        [
            lambda c: c.s["role"].create(
                c.admin, name="Other", slug="audit_other", permission_slugs=[ORDERS_VIEW]
            )
        ],
        None,
        {"slug": "audit_other", "permissions": [ORDERS_VIEW]},
    ),
    (
        "iam.role.rename",
        [_t("role.rename", ROLE, "Renamed")],
        {"name": "Viewer"},
        {"name": "Renamed"},
    ),
    (
        "iam.role.change_slug",
        [_t("role.change_slug", ROLE, "audit_renamed")],
        {"slug": ROLE_SLUG},
        {"slug": "audit_renamed"},
    ),
    (
        "iam.role.deactivate",
        [_t("role.deactivate", ROLE)],
        {"is_active": True},
        {"is_active": False},
    ),
    (
        "iam.role.activate",
        [_t("role.deactivate", ROLE), _t("role.activate", ROLE)],
        {"is_active": False},
        {"is_active": True},
    ),
    (
        "iam.role.delete",
        [_t("role.soft_delete", ROLE)],
        {"is_deleted": False},
        {"is_deleted": True},
    ),
    (
        "iam.role.set_permissions",
        [_t("role.set_permissions", ROLE, [PRODUCTS_VIEW, ORDERS_VIEW])],
        {"permissions": [PRODUCTS_VIEW]},
        {"permissions": sorted([ORDERS_VIEW, PRODUCTS_VIEW])},
    ),
    ("iam.user_role.assign", [_t("access.assign_role", TARGET, ROLE)], None, {"role": ROLE_SLUG}),
    (
        "iam.user_role.revoke",
        [_t("access.assign_role", TARGET, ROLE), _t("access.revoke_role", TARGET, ROLE)],
        {"role": ROLE_SLUG},
        None,
    ),
    (
        "iam.override.upsert",
        [_t("access.upsert_override", TARGET, ORDERS_VIEW, is_granted=False)],
        None,
        {"permission": ORDERS_VIEW, "is_granted": False},
    ),
    (
        "iam.override.remove",
        [
            _t("access.upsert_override", TARGET, ORDERS_VIEW, is_granted=False),
            _t("access.remove_override", TARGET, ORDERS_VIEW),
        ],
        {"permission": ORDERS_VIEW, "is_granted": False},
        None,
    ),
    ("iam.moderator.assign", [_t("access.assign_moderator", TARGET, TENANT)], None, {}),
    (
        "iam.moderator.unassign",
        [
            _t("access.assign_moderator", TARGET, TENANT),
            _t("access.unassign_moderator", TARGET, TENANT),
        ],
        {},
        None,
    ),
]


def _matches(actual: Any, expected: dict[str, Any] | None) -> bool:
    if expected is None:
        return actual is None
    return isinstance(actual, dict) and all(actual.get(k) == v for k, v in expected.items())


@pytest.mark.parametrize(
    ("action", "steps", "before", "after"), SCENARIOS, ids=[s[0] for s in SCENARIOS]
)
async def test_mutation_writes_one_audit_row_and_stays_consistent(
    session: AsyncSession,
    ctx: Ctx,
    perms: PermissionService,  # noqa: F811
    action: str,
    steps: list[Step],
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    await _seq(ctx, *steps)
    rows = (
        await session.scalars(
            select(AuditLog).where(AuditLog.action == action, AuditLog.actor_id == ctx.admin)
        )
    ).all()
    assert len(rows) == 1, action
    row = rows[0]
    assert row.actor_type == "user" and row.result == "success"
    assert row.payload is not None
    assert _matches(row.payload["before"], before), row.payload
    assert _matches(row.payload["after"], after), row.payload
    for service in ctx.s.values():
        assert service.cache_invalidated is True
    await assert_consistent(perms, ctx.target)
    await assert_consistent(perms, ctx.admin)


async def test_update_username_audited_by_target(session: AsyncSession, ctx: Ctx) -> None:
    user = await ctx.s["user"].update_username(ctx.target, "@renamed")
    assert user.telegram_user_name == "renamed"
    rows = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.action == "iam.user.update_username", AuditLog.actor_id == ctx.target
            )
        )
    ).all()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload is not None
    assert payload["before"]["telegram_user_name"] is None
    assert payload["after"]["telegram_user_name"] == "renamed"


async def test_no_op_mutations_write_no_audit(session: AsyncSession, ctx: Ctx) -> None:
    await ctx.s["tenant"].update(ctx.admin, ctx.tenant, name="Shop")
    await ctx.s["tenant"].activate(ctx.admin, ctx.tenant)
    await ctx.s["tenant"].update_settings(ctx.admin, ctx.tenant, {"is_store_open": False})
    await ctx.s["role"].rename(ctx.admin, ctx.role, "Viewer")
    await ctx.s["role"].set_permissions(ctx.admin, ctx.role, [PRODUCTS_VIEW])
    await ctx.s["user"].activate(ctx.admin, ctx.target)
    await ctx.s["user"].update_username(ctx.target, None)
    await ctx.s["access"].upsert_override(ctx.admin, ctx.target, ORDERS_VIEW, is_granted=False)
    await ctx.s["access"].upsert_override(ctx.admin, ctx.target, ORDERS_VIEW, is_granted=False)
    actions = (await session.scalars(select(AuditLog.action))).all()
    assert [a for a in actions if a.startswith("iam.")] == ["iam.override.upsert"]


async def _code(call: Awaitable[Any]) -> str | None:
    try:
        await call
    except DomainError as error:
        return error.code
    return None


async def test_error_codes(session: AsyncSession, ctx: Ctx) -> None:
    s, a, t = ctx.s, ctx.admin, ctx.target
    assert await _code(s["user"].add_by_telegram_id(a, t)) == svc.USER_EXISTS
    assert await _code(s["role"].create(a, name="X", slug=ROLE_SLUG)) == svc.ROLE_SLUG_TAKEN
    assert await _code(s["role"].change_slug(a, ctx.role, "admin")) == svc.ROLE_SLUG_TAKEN
    assert await _code(s["access"].revoke_role(a, t, ctx.role)) == svc.ROLE_NOT_ASSIGNED
    await s["access"].assign_role(a, t, ctx.role)
    assert await _code(s["access"].assign_role(a, t, ctx.role)) == svc.ROLE_ALREADY_ASSIGNED
    assert await _code(s["access"].unassign_moderator(a, t, ctx.tenant)) == svc.MODERATOR_NOT_FOUND
    await s["access"].assign_moderator(a, t, ctx.tenant)
    assert (
        await _code(s["access"].assign_moderator(a, t, ctx.tenant))
        == svc.MODERATOR_ALREADY_ASSIGNED
    )
    assert await _code(s["access"].remove_override(a, t, ORDERS_VIEW)) == svc.OVERRIDE_NOT_FOUND
    assert await _code(s["tenant"].update(a, uuid4(), name="X")) == svc.TENANT_NOT_FOUND
    assert (
        await _code(s["access"].upsert_override(a, t, "nope.nope", is_granted=False))
        == svc.PERMISSION_NOT_FOUND
    )
    assert (
        await _code(s["role"].set_permissions(a, ctx.role, ["nope.nope"]))
        == svc.PERMISSION_NOT_FOUND
    )
    assert await _code(s["tenant"].update_settings(a, ctx.tenant, {"faq": 1})) == (
        svc.INVALID_SETTINGS
    )
    assert (
        await _code(s["tenant"].update_settings(a, ctx.tenant, {"default_language_code": "zz"}))
        == svc.LANGUAGE_NOT_FOUND
    )
    assert await _code(s["user"].add_by_telegram_id(a, tg_id(), None)) is None
    await s["user"].soft_delete(a, t)
    assert await _code(s["user"].add_by_telegram_id(a, t)) == svc.USER_DELETED


async def _limited_actor(session: AsyncSession) -> Any:
    """Holds only ``roles.manage`` (allow-override)."""
    limited = await make_user(session)
    await direct_override(session, limited.id, ROLES_MANAGE, granted=True)
    return limited


async def test_removing_deny_requires_holding_permission(session: AsyncSession, ctx: Ctx) -> None:
    limited = await _limited_actor(session)
    target_id = (
        await session.scalars(select(User.id).where(User.telegram_user_id == ctx.target))
    ).one()
    await direct_override(session, target_id, PRODUCTS_VIEW, granted=False)
    with pytest.raises(PermissionDeniedError) as info:
        await ctx.s["access"].remove_override(limited.telegram_user_id, ctx.target, PRODUCTS_VIEW)
    assert info.value.code == svc.PRIVILEGE_ESCALATION
    await ctx.s["access"].remove_override(ctx.admin, ctx.target, PRODUCTS_VIEW)


async def test_activation_requires_holding_target_rights(session: AsyncSession, ctx: Ctx) -> None:
    limited = await _limited_actor(session)
    target = await make_user(session, role=ROLE_MODERATOR)
    await ctx.s["user"].deactivate(ctx.admin, target.telegram_user_id)
    with pytest.raises(PermissionDeniedError) as info:
        await ctx.s["user"].activate(limited.telegram_user_id, target.telegram_user_id)
    assert info.value.code == svc.PRIVILEGE_ESCALATION
    await ctx.s["user"].activate(ctx.admin, target.telegram_user_id)


async def test_activation_ignores_deleted_shop_and_denied_role_rights(
    session: AsyncSession, ctx: Ctx
) -> None:
    """R2: grants the target does not effectively hold are not required from the actor."""
    limited = await _limited_actor(session)
    s, a, t = ctx.s, ctx.admin, ctx.target
    await s["access"].upsert_override(a, t, PRODUCTS_VIEW, is_granted=True, tenant_id=ctx.tenant)
    await s["tenant"].soft_delete(a, ctx.tenant)  # (a) allow in a deleted shop
    await s["access"].assign_role(a, t, ctx.role)
    await s["access"].upsert_override(a, t, PRODUCTS_VIEW, is_granted=False)  # (b) global deny
    await s["user"].deactivate(a, t)
    await s["user"].activate(limited.telegram_user_id, t)


async def test_inactive_role_permission_does_not_block_assignment(
    session: AsyncSession, ctx: Ctx
) -> None:
    limited = await _limited_actor(session)
    await direct_override(session, limited.id, PRODUCTS_VIEW, granted=True)
    session.add(
        RolePermission(role_id=ctx.role, permission_id=await _permission_id(session, ORDERS_VIEW))
    )
    orders = (await session.scalars(select(Permission).where(Permission.slug == ORDERS_VIEW))).one()
    orders.is_active = False
    await session.commit()
    await ctx.s["access"].assign_role(limited.telegram_user_id, ctx.target, ctx.role)


async def test_override_of_inactive_permission_can_be_removed(
    session: AsyncSession, ctx: Ctx
) -> None:
    await ctx.s["access"].upsert_override(ctx.admin, ctx.target, ORDERS_VIEW, is_granted=False)
    orders = (await session.scalars(select(Permission).where(Permission.slug == ORDERS_VIEW))).one()
    orders.is_active = False
    await session.commit()
    await ctx.s["access"].remove_override(ctx.admin, ctx.target, ORDERS_VIEW)


async def test_change_slug_invalidates_all(ctx: Ctx, redis_client: Any) -> None:
    before = int(await redis_client.get(GLOBAL_GENERATION_KEY) or 0)
    await ctx.s["role"].change_slug(ctx.admin, ctx.role, "audit_slug2")
    assert int(await redis_client.get(GLOBAL_GENERATION_KEY) or 0) == before + 1


async def test_foreign_pending_changes_rejected(session: AsyncSession, ctx: Ctx) -> None:
    session.add(Role(name="Foreign", slug="foreign_pending"))
    with pytest.raises(DomainError) as info:
        await ctx.s["role"].rename(ctx.admin, ctx.role, "X")
    assert info.value.code == svc.SESSION_NOT_CLEAN
    await session.rollback()
