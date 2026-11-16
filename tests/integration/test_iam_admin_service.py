"""IAM write services against real PostgreSQL (savepoint session) and test Redis."""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam import admin_service as svc
from app.modules.iam.admin_service import (
    AccessAdminService,
    RoleAdminService,
    TenantAdminService,
    UserAdminService,
)
from app.modules.iam.cache import PermissionCache
from app.modules.iam.models import (
    Permission,
    Role,
    TenantModerator,
    TenantSettings,
    User,
    UserPermissionOverride,
    UserRole,
)
from app.modules.iam.permissions import (
    MODERATORS_MANAGE,
    ORDERS_VIEW,
    PRODUCTS_EDIT,
    PRODUCTS_VIEW,
    ROLE_ADMIN,
    ROLE_MODERATOR,
    ROLE_SENIOR_MODERATOR,
    ROLES_MANAGE,
    SETTINGS_MANAGE,
    TENANTS_VIEW_ALL,
)
from app.modules.iam.service import PERMISSION_DENIED, PermissionService
from app.modules.logging.models import AuditLog
from app.shared_kernel.domain.exceptions import (
    DomainError,
    EntityAlreadyExistsError,
    PermissionDeniedError,
    ValidationError,
)

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.2")]

_IDS = itertools.count(2_000_000_000 + uuid.uuid4().int % 10**8)


def tg_id() -> int:
    return next(_IDS)


@pytest.fixture
def perms(session: AsyncSession, redis_client: Any) -> PermissionService:
    return PermissionService(session, PermissionCache(redis_client))


@pytest.fixture
def services(session: AsyncSession, perms: PermissionService) -> dict[str, Any]:
    return {
        "tenant": TenantAdminService(session, perms),
        "user": UserAdminService(session, perms),
        "role": RoleAdminService(session, perms),
        "access": AccessAdminService(session, perms),
    }


@dataclass(frozen=True, slots=True)
class Ref:
    """Plain ids: a failed service call rolls the savepoint back and expires ORM objects."""

    id: uuid.UUID
    telegram_user_id: int = 0
    slug: str = ""


async def make_user(
    session: AsyncSession, *, role: str | None = None, superuser: bool = False
) -> Ref:
    """Committed (released savepoint) user: survives a rollback of a later failed call."""
    user = User(telegram_user_id=tg_id(), is_superuser=superuser)
    session.add(user)
    await session.flush()
    if role is not None:
        role_row = (await session.scalars(select(Role).where(Role.slug == role))).one()
        session.add(UserRole(user_id=user.id, role_id=role_row.id))
    await session.commit()
    return Ref(user.id, user.telegram_user_id)


async def role_id(session: AsyncSession, slug: str) -> uuid.UUID:
    return (await session.scalars(select(Role.id).where(Role.slug == slug))).one()


async def audit_count(session: AsyncSession, action: str, actor: int) -> int:
    stmt = (
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == action, AuditLog.actor_id == actor)
    )
    return int(await session.scalar(stmt) or 0)


async def assert_consistent(perms: PermissionService, telegram_user_id: int) -> None:
    """Report entries == live ``check`` for the committed state (fresh snapshot)."""
    report = await perms.effective_permissions(telegram_user_id)
    for item in report.permissions:
        decision = await perms.check(telegram_user_id, item.permission_slug, item.tenant_id)
        assert (item.allowed, item.source) == (decision.allowed, decision.source), item


async def test_create_tenant_creates_settings_and_audit(
    session: AsyncSession, services: dict[str, Any]
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    tenant_row = await services["tenant"].create(
        admin.telegram_user_id, name="Shop", slug=f"s-{uuid.uuid4().hex[:8]}"
    )
    tenant = Ref(tenant_row.id, slug=tenant_row.slug)
    settings = (
        await session.scalars(select(TenantSettings).where(TenantSettings.tenant_id == tenant.id))
    ).one()
    assert settings.is_store_open is False
    assert await audit_count(session, "iam.tenant.create", admin.telegram_user_id) == 1
    with pytest.raises(EntityAlreadyExistsError) as info:
        await services["tenant"].create(admin.telegram_user_id, name="X", slug=tenant.slug)
    assert info.value.code == svc.TENANT_SLUG_TAKEN

    await services["tenant"].update_settings(
        admin.telegram_user_id, tenant.id, {"is_store_open": True, "default_language_code": "en"}
    )
    assert await audit_count(session, "iam.tenant.settings_update", admin.telegram_user_id) == 1
    with pytest.raises(ValidationError):
        await services["tenant"].update_settings(
            admin.telegram_user_id, tenant.id, {"default_language_code": "zz"}
        )


async def test_mutation_requires_permission_and_writes_nothing(
    session: AsyncSession, services: dict[str, Any]
) -> None:
    senior = await make_user(session, role=ROLE_SENIOR_MODERATOR)  # no roles/settings.manage
    with pytest.raises(PermissionDeniedError):
        await services["role"].create(senior.telegram_user_id, name="R", slug="r_denied")
    with pytest.raises(PermissionDeniedError):
        await services["tenant"].create(senior.telegram_user_id, name="S", slug="denied-shop")
    assert (
        await session.scalar(select(func.count()).select_from(Role).where(Role.slug == "r_denied"))
        == 0
    )


async def test_self_assignment_and_self_override_forbidden(
    session: AsyncSession, services: dict[str, Any]
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    tg = admin.telegram_user_id
    with pytest.raises(PermissionDeniedError) as info:
        await services["access"].assign_role(tg, tg, await role_id(session, ROLE_MODERATOR))
    assert info.value.code == svc.SELF_MODIFICATION_FORBIDDEN
    with pytest.raises(PermissionDeniedError) as info:
        await services["access"].upsert_override(tg, tg, PRODUCTS_VIEW, is_granted=True)
    assert info.value.code == svc.SELF_MODIFICATION_FORBIDDEN


async def test_global_permission_tenant_override_rejected(
    session: AsyncSession, services: dict[str, Any]
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    target = await make_user(session)
    tenant_row = await services["tenant"].create(
        admin.telegram_user_id, name="S", slug=f"g-{uuid.uuid4().hex[:8]}"
    )
    tenant = Ref(tenant_row.id, slug=tenant_row.slug)
    for slug in (ROLES_MANAGE, SETTINGS_MANAGE, TENANTS_VIEW_ALL, MODERATORS_MANAGE):
        with pytest.raises(ValidationError) as info:
            await services["access"].upsert_override(
                admin.telegram_user_id,
                target.telegram_user_id,
                slug,
                is_granted=True,
                tenant_id=tenant.id,
            )
        assert info.value.code == svc.GLOBAL_PERMISSION_TENANT_OVERRIDE


async def test_expires_at_in_past_rejected_and_expired_slot_upserted(
    session: AsyncSession, services: dict[str, Any], perms: PermissionService
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    target = await make_user(session, role=ROLE_SENIOR_MODERATOR)
    with pytest.raises(ValidationError) as info:
        await services["access"].upsert_override(
            admin.telegram_user_id,
            target.telegram_user_id,
            ORDERS_VIEW,
            is_granted=False,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    assert info.value.code == svc.EXPIRES_AT_NOT_IN_FUTURE

    permission = (
        await session.scalars(select(Permission).where(Permission.slug == ORDERS_VIEW))
    ).one()
    session.add(
        UserPermissionOverride(
            user_id=target.id,
            permission_id=permission.id,
            is_granted=False,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await session.commit()
    assert await perms.has_permission(target.telegram_user_id, ORDERS_VIEW)

    row = await services["access"].upsert_override(
        admin.telegram_user_id,
        target.telegram_user_id,
        ORDERS_VIEW,
        is_granted=False,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        reason="test",
    )
    count = await session.scalar(
        select(func.count())
        .select_from(UserPermissionOverride)
        .where(UserPermissionOverride.user_id == target.id)
    )
    assert count == 1 and row.is_granted is False
    # deny beats the role allow; cache was invalidated after commit
    assert not await perms.has_permission(target.telegram_user_id, ORDERS_VIEW)
    assert await audit_count(session, "iam.override.upsert", admin.telegram_user_id) == 1
    await assert_consistent(perms, target.telegram_user_id)

    await services["access"].remove_override(
        admin.telegram_user_id, target.telegram_user_id, ORDERS_VIEW
    )
    assert await perms.has_permission(target.telegram_user_id, ORDERS_VIEW)
    assert await audit_count(session, "iam.override.remove", admin.telegram_user_id) == 1


async def test_system_role_protected(session: AsyncSession, services: dict[str, Any]) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    rid = await role_id(session, ROLE_MODERATOR)
    role_service: RoleAdminService = services["role"]
    for call in (
        role_service.deactivate(admin.telegram_user_id, rid),
        role_service.soft_delete(admin.telegram_user_id, rid),
        role_service.change_slug(admin.telegram_user_id, rid, "mod2"),
        role_service.set_permissions(admin.telegram_user_id, rid, [PRODUCTS_VIEW]),
    ):
        with pytest.raises(ValidationError) as info:
            await call
        assert info.value.code == svc.SYSTEM_ROLE_PROTECTED
    role = await role_service.rename(admin.telegram_user_id, rid, "Модератор магазина")
    assert role.slug == ROLE_MODERATOR and role.is_active


async def test_custom_role_lifecycle_consistent_and_audited(
    session: AsyncSession, services: dict[str, Any], perms: PermissionService
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    target = await make_user(session)
    tg = target.telegram_user_id
    slug = f"viewer_{uuid.uuid4().hex[:6]}"
    role = await services["role"].create(
        admin.telegram_user_id, name="Viewer", slug=slug, permission_slugs=[PRODUCTS_VIEW]
    )
    await services["access"].assign_role(admin.telegram_user_id, tg, role.id)
    assert await perms.has_permission(tg, PRODUCTS_VIEW)
    await assert_consistent(perms, tg)

    await services["role"].set_permissions(admin.telegram_user_id, role.id, [PRODUCTS_EDIT])
    assert not await perms.has_permission(tg, PRODUCTS_VIEW)
    assert await perms.has_permission(tg, PRODUCTS_EDIT)
    await assert_consistent(perms, tg)

    await services["role"].deactivate(admin.telegram_user_id, role.id)
    assert not await perms.has_permission(tg, PRODUCTS_EDIT)
    await assert_consistent(perms, tg)
    await services["access"].revoke_role(admin.telegram_user_id, tg, role.id)
    for action in (
        "iam.role.create",
        "iam.user_role.assign",
        "iam.role.set_permissions",
        "iam.role.deactivate",
        "iam.user_role.revoke",
    ):
        assert await audit_count(session, action, admin.telegram_user_id) == 1, action


async def test_privilege_escalation_blocked(
    session: AsyncSession, services: dict[str, Any]
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    limited = await make_user(session)  # roles.manage only, via override
    perm = (await session.scalars(select(Permission).where(Permission.slug == ROLES_MANAGE))).one()
    session.add(
        UserPermissionOverride(
            user_id=limited.id, permission_id=perm.id, is_granted=True, granted_by_id=admin.id
        )
    )
    await session.commit()
    target = await make_user(session)
    with pytest.raises(PermissionDeniedError) as info:
        await services["access"].upsert_override(
            limited.telegram_user_id, target.telegram_user_id, PRODUCTS_VIEW, is_granted=True
        )
    assert info.value.code == svc.PRIVILEGE_ESCALATION


async def test_soft_delete_user_removes_assignments(
    session: AsyncSession, services: dict[str, Any], perms: PermissionService
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    tenant_row = await services["tenant"].create(
        admin.telegram_user_id, name="S", slug=f"d-{uuid.uuid4().hex[:8]}"
    )
    tenant = Ref(tenant_row.id, slug=tenant_row.slug)
    target = await services["user"].add_by_telegram_id(admin.telegram_user_id, tg_id(), "@staff")
    tg = target.telegram_user_id
    assert target.telegram_user_name == "staff" and target.is_superuser is False
    await services["access"].assign_role(
        admin.telegram_user_id, tg, await role_id(session, ROLE_MODERATOR)
    )
    await services["access"].assign_moderator(admin.telegram_user_id, tg, tenant.id)
    await services["access"].upsert_override(
        admin.telegram_user_id, tg, PRODUCTS_EDIT, is_granted=False, tenant_id=tenant.id
    )
    assert await perms.has_permission(tg, PRODUCTS_VIEW, tenant.id)

    await services["user"].soft_delete(admin.telegram_user_id, tg)
    for model in (UserRole, UserPermissionOverride, TenantModerator):
        n = await session.scalar(
            select(func.count()).select_from(model).where(model.user_id == target.id)
        )
        assert n == 0, model
    assert not await perms.has_permission(tg, PRODUCTS_VIEW, tenant.id)
    await services["user"].restore(admin.telegram_user_id, tg)
    assert not await perms.has_permission(tg, PRODUCTS_VIEW, tenant.id)  # rights not restored
    await assert_consistent(perms, tg)
    assert await audit_count(session, "iam.user.delete", admin.telegram_user_id) == 1
    assert await audit_count(session, "iam.user.restore", admin.telegram_user_id) == 1
    with pytest.raises(PermissionDeniedError):
        await services["user"].soft_delete(admin.telegram_user_id, admin.telegram_user_id)


async def test_moderator_assignment_requires_moderators_manage(
    session: AsyncSession, services: dict[str, Any], perms: PermissionService
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    nobody = await make_user(session)
    target = await make_user(session, role=ROLE_MODERATOR)
    tenant_row = await services["tenant"].create(
        admin.telegram_user_id, name="S", slug=f"m-{uuid.uuid4().hex[:8]}"
    )
    tenant = Ref(tenant_row.id, slug=tenant_row.slug)
    with pytest.raises(PermissionDeniedError):
        await services["access"].assign_moderator(
            nobody.telegram_user_id, target.telegram_user_id, tenant.id
        )
    senior = await make_user(session, role=ROLE_SENIOR_MODERATOR)  # no moderators.manage
    with pytest.raises(PermissionDeniedError):
        await services["access"].assign_moderator(
            senior.telegram_user_id, target.telegram_user_id, tenant.id
        )
    senior = await make_user(session, role=ROLE_ADMIN)
    await services["access"].assign_moderator(
        senior.telegram_user_id, target.telegram_user_id, tenant.id
    )
    assert await perms.has_permission(target.telegram_user_id, PRODUCTS_VIEW, tenant.id)
    await assert_consistent(perms, target.telegram_user_id)
    await services["access"].unassign_moderator(
        senior.telegram_user_id, target.telegram_user_id, tenant.id
    )
    assert not await perms.has_permission(target.telegram_user_id, PRODUCTS_VIEW, tenant.id)
    assert await audit_count(session, "iam.moderator.assign", senior.telegram_user_id) == 1
    assert await audit_count(session, "iam.moderator.unassign", senior.telegram_user_id) == 1


async def test_superuser_target_protected_and_username_update(
    session: AsyncSession, services: dict[str, Any]
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    root = await make_user(session, superuser=True)
    with pytest.raises(PermissionDeniedError) as info:
        await services["user"].deactivate(admin.telegram_user_id, root.telegram_user_id)
    assert info.value.code == svc.SUPERUSER_PROTECTED
    user = await services["user"].update_username(admin.telegram_user_id, "@new_name")
    assert user is not None and user.telegram_user_name == "new_name"
    assert await services["user"].update_username(tg_id(), "ghost") is None


# --- final role model: tenants.manage, global access is never assigned to shops ---------


async def _unassign_audits(session: AsyncSession, user_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(AuditLog.payload).where(
            AuditLog.action == "iam.moderator.unassign", AuditLog.entity_id == user_id
        )
    )
    # Service audit payload: {"before": {...}, "after": None}.
    return [dict((r or {}).get("before") or {}) for r in rows.all()]


async def _assignments(session: AsyncSession, user_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(TenantModerator)
            .where(TenantModerator.user_id == user_id)
        )
        or 0
    )


async def _shop(services: dict[str, Any], admin: Ref) -> Ref:
    row = await services["tenant"].create(
        admin.telegram_user_id, name="S", slug=f"ga-{uuid.uuid4().hex[:8]}"
    )
    return Ref(row.id, slug=row.slug)


async def test_settings_manage_alone_does_not_manage_tenants(
    session: AsyncSession, services: dict[str, Any]
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    shop = await _shop(services, admin)
    system = await make_user(session)
    await services["access"].upsert_override(
        admin.telegram_user_id, system.telegram_user_id, SETTINGS_MANAGE, is_granted=True
    )
    tg = system.telegram_user_id
    for call in (
        services["tenant"].create(tg, name="S", slug=f"no-{uuid.uuid4().hex[:8]}"),
        services["tenant"].update_settings(tg, shop.id, {"default_language_code": None}),
        services["tenant"].soft_delete(tg, shop.id),
    ):
        with pytest.raises(PermissionDeniedError) as info:
            await call
        assert info.value.code == PERMISSION_DENIED


@pytest.mark.parametrize("kind", ["senior", "superuser"])
@pytest.mark.req("R-II.2", "R-II.4")
async def test_global_access_is_never_assigned_to_shops(
    session: AsyncSession, services: dict[str, Any], kind: str
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    shop = await _shop(services, admin)
    target = await (
        make_user(session, role=ROLE_SENIOR_MODERATOR)
        if kind == "senior"
        else make_user(session, superuser=True)
    )
    with pytest.raises(DomainError) as info:
        await services["access"].assign_moderator(
            admin.telegram_user_id, target.telegram_user_id, shop.id
        )
    assert info.value.code in {svc.MODERATOR_HAS_GLOBAL_ACCESS, svc.SUPERUSER_PROTECTED}
    if kind == "senior":
        assert info.value.code == svc.MODERATOR_HAS_GLOBAL_ACCESS
    assert await _assignments(session, target.id) == 0


@pytest.mark.req("R-II.2", "R-II.4")
async def test_senior_role_drops_assignments_with_audit(
    session: AsyncSession, services: dict[str, Any]
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    shop = await _shop(services, admin)
    target = await make_user(session, role=ROLE_MODERATOR)
    await services["access"].assign_moderator(
        admin.telegram_user_id, target.telegram_user_id, shop.id
    )
    await services["access"].assign_role(
        admin.telegram_user_id,
        target.telegram_user_id,
        await role_id(session, ROLE_SENIOR_MODERATOR),
    )
    assert await _assignments(session, target.id) == 0
    audits = await _unassign_audits(session, target.id)
    assert [(a["reason"], a["tenant_id"]) for a in audits] == [("global_access", str(shop.id))]


@pytest.mark.req("R-II.2", "R-II.4")
async def test_global_view_all_override_drops_assignments(
    session: AsyncSession, services: dict[str, Any]
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    shop = await _shop(services, admin)
    target = await make_user(session, role=ROLE_MODERATOR)
    await services["access"].assign_moderator(
        admin.telegram_user_id, target.telegram_user_id, shop.id
    )
    await services["access"].upsert_override(
        admin.telegram_user_id, target.telegram_user_id, TENANTS_VIEW_ALL, is_granted=True
    )
    assert await _assignments(session, target.id) == 0
    assert [a["reason"] for a in await _unassign_audits(session, target.id)] == ["global_access"]
    with pytest.raises(ValidationError) as info:
        await services["access"].assign_moderator(
            admin.telegram_user_id, target.telegram_user_id, shop.id
        )
    assert info.value.code == svc.MODERATOR_HAS_GLOBAL_ACCESS


@pytest.mark.req("R-II.2", "R-II.4")
async def test_superuser_grant_drops_assignments(
    session: AsyncSession, services: dict[str, Any], perms: PermissionService
) -> None:
    from app.modules.iam.superuser import SuperuserInstaller

    admin = await make_user(session, role=ROLE_ADMIN)
    shop = await _shop(services, admin)
    target = await make_user(session, role=ROLE_MODERATOR)
    await services["access"].assign_moderator(
        admin.telegram_user_id, target.telegram_user_id, shop.id
    )
    result = await SuperuserInstaller(session, perms).grant(target.telegram_user_id)
    assert result.changed
    assert await _assignments(session, target.id) == 0
    actors = await session.scalars(
        select(AuditLog.actor_type).where(
            AuditLog.action == "iam.moderator.unassign", AuditLog.entity_id == target.id
        )
    )
    assert actors.all() == ["system"]


# --- every path to a global tenants.view_all drops shop assignments (review F1/F2) ----


async def _moderated(
    session: AsyncSession, services: dict[str, Any], admin: Ref
) -> tuple[Ref, Ref]:
    shop = await _shop(services, admin)
    target = await make_user(session, role=ROLE_MODERATOR)
    await services["access"].assign_moderator(
        admin.telegram_user_id, target.telegram_user_id, shop.id
    )
    return target, shop


async def _custom_role(services: dict[str, Any], admin: Ref, *slugs: str) -> uuid.UUID:
    role = await services["role"].create(
        admin.telegram_user_id, name="Custom", slug=f"r_{uuid.uuid4().hex[:8]}"
    )
    role_uuid: uuid.UUID = role.id
    await services["role"].set_permissions(admin.telegram_user_id, role_uuid, list(slugs))
    return role_uuid


@pytest.mark.req("R-II.2", "R-II.4")
async def test_role_gaining_view_all_drops_assignments(
    session: AsyncSession, services: dict[str, Any]
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    role = await _custom_role(services, admin, PRODUCTS_VIEW)
    target, shop = await _moderated(session, services, admin)
    await services["access"].assign_role(admin.telegram_user_id, target.telegram_user_id, role)
    assert await _assignments(session, target.id) == 1
    await services["role"].set_permissions(
        admin.telegram_user_id, role, [PRODUCTS_VIEW, TENANTS_VIEW_ALL]
    )
    assert await _assignments(session, target.id) == 0
    audits = await _unassign_audits(session, target.id)
    assert [(a["reason"], a["tenant_id"]) for a in audits] == [("global_access", str(shop.id))]


@pytest.mark.req("R-II.2", "R-II.4")
async def test_role_activation_with_view_all_drops_assignments(
    session: AsyncSession, services: dict[str, Any]
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    role = await _custom_role(services, admin, PRODUCTS_VIEW)
    target = await make_user(session, role=ROLE_MODERATOR)
    await services["access"].assign_role(admin.telegram_user_id, target.telegram_user_id, role)
    await services["role"].deactivate(admin.telegram_user_id, role)
    await services["role"].set_permissions(
        admin.telegram_user_id, role, [PRODUCTS_VIEW, TENANTS_VIEW_ALL]
    )
    shop = await _shop(services, admin)
    await services["access"].assign_moderator(
        admin.telegram_user_id, target.telegram_user_id, shop.id
    )  # inactive role: no global access yet
    assert await _assignments(session, target.id) == 1
    await services["role"].activate(admin.telegram_user_id, role)
    assert await _assignments(session, target.id) == 0
    assert [a["reason"] for a in await _unassign_audits(session, target.id)] == ["global_access"]


@pytest.mark.req("R-II.2", "R-II.4")
async def test_global_deny_keeps_assignments_until_removed(
    session: AsyncSession, services: dict[str, Any]
) -> None:
    admin = await make_user(session, role=ROLE_ADMIN)
    target, shop = await _moderated(session, services, admin)
    tg = target.telegram_user_id
    await services["access"].upsert_override(
        admin.telegram_user_id, tg, TENANTS_VIEW_ALL, is_granted=False
    )
    await services["access"].assign_role(
        admin.telegram_user_id, tg, await role_id(session, ROLE_SENIOR_MODERATOR)
    )
    assert await _assignments(session, target.id) == 1  # deny wins: no global access
    assert await _unassign_audits(session, target.id) == []
    await services["access"].remove_override(admin.telegram_user_id, tg, TENANTS_VIEW_ALL)
    assert await _assignments(session, target.id) == 0
    audits = await _unassign_audits(session, target.id)
    assert [(a["reason"], a["tenant_id"]) for a in audits] == [("global_access", str(shop.id))]
