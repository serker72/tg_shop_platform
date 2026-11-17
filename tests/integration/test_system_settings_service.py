"""``SystemSettingsService`` against real PostgreSQL (savepoint session) and test Redis."""

from __future__ import annotations

import itertools
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.modules.iam.cache import PermissionCache
from app.modules.iam.models import Role, User, UserRole
from app.modules.iam.permissions import ROLE_MODERATOR
from app.modules.iam.service import PERMISSION_DENIED, PermissionService
from app.modules.logging import retention
from app.modules.logging.models import AuditLog
from app.modules.system import service as svc
from app.modules.system.maintenance import MaintenanceGate
from app.modules.system.models import SystemSettings
from app.modules.system.service import SystemSettingsService
from app.shared_kernel.domain.exceptions import (
    DomainError,
    PermissionDeniedError,
    ValidationError,
)

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.5")]

_IDS = itertools.count(3_000_000_000 + uuid.uuid4().int % 10**8)
RETENTION_SQL = text(
    "SELECT parent_table, retention::interval = make_interval(months => "
    "CASE parent_table WHEN 'public.user_activity' THEN :ua ELSE :audit END) "
    "FROM partman.part_config WHERE parent_table IN "
    "('public.user_activity', 'public.audit_logs')"
)


@pytest.fixture
def gate(session_factory: async_sessionmaker[AsyncSession]) -> MaintenanceGate:
    return MaintenanceGate(session_factory, ttl=3600)


@pytest.fixture
def service(
    session: AsyncSession, redis_client: Any, gate: MaintenanceGate
) -> SystemSettingsService:
    return SystemSettingsService(
        session, PermissionService(session, PermissionCache(redis_client)), gate=gate
    )


async def make_user(
    session: AsyncSession, *, superuser: bool = False, role: str | None = None
) -> int:
    user = User(telegram_user_id=next(_IDS), is_superuser=superuser)
    session.add(user)
    await session.flush()
    if role is not None:
        role_row = (await session.scalars(select(Role).where(Role.slug == role))).one()
        session.add(UserRole(user_id=user.id, role_id=role_row.id))
    telegram_id = user.telegram_user_id
    await session.commit()
    return telegram_id


async def audit_count(session: AsyncSession, action: str, actor: int) -> int:
    stmt = (
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == action, AuditLog.actor_id == actor)
    )
    return int(await session.scalar(stmt) or 0)


async def current(session: AsyncSession) -> tuple[Any, ...]:
    row = (
        await session.execute(
            select(
                SystemSettings.user_activity_retention_months,
                SystemSettings.audit_logs_retention_months,
                SystemSettings.file_logs_retention_days,
                SystemSettings.iam_cache_ttl_seconds,
            )
        )
    ).one()
    return tuple(row)


async def retention_synced(session: AsyncSession, ua: int, audit: int) -> bool:
    rows = (await session.execute(RETENTION_SQL, {"ua": ua, "audit": audit})).all()
    return len(rows) == 2 and all(row[1] for row in rows)


async def test_set_retention_syncs_part_config_and_audits(
    session: AsyncSession, service: SystemSettingsService
) -> None:
    admin = await make_user(session, superuser=True)
    await service.set_retention(admin, 5, 36)
    assert (await current(session))[:2] == (5, 36)
    assert await retention_synced(session, 5, 36)
    assert await audit_count(session, "system.settings.retention", admin) == 1
    payload = (
        await session.scalars(
            select(AuditLog.payload).where(
                AuditLog.action == "system.settings.retention", AuditLog.actor_id == admin
            )
        )
    ).one()
    assert payload["after"] == {
        "user_activity_retention_months": 5,
        "audit_logs_retention_months": 36,
    }
    assert {item["table"] for item in payload["partman"]} == {
        "public.user_activity",
        "public.audit_logs",
    }
    await service.set_retention(admin, 5, 36)  # no change: no audit
    assert service._noop is True
    assert await audit_count(session, "system.settings.retention", admin) == 1


async def test_partial_retention_keeps_other_column(
    session: AsyncSession, service: SystemSettingsService
) -> None:
    admin = await make_user(session, superuser=True)
    await service.set_retention(admin, 4, 30)
    await service.set_retention(admin, audit_logs_months=48)
    assert (await current(session))[:2] == (4, 48)
    assert await retention_synced(session, 4, 48)
    await service.set_retention(admin, user_activity_months=6)
    assert (await current(session))[:2] == (6, 48)
    assert await retention_synced(session, 6, 48)
    payload = (
        await session.scalars(
            select(AuditLog.payload)
            .where(AuditLog.action == "system.settings.retention", AuditLog.actor_id == admin)
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    ).one()
    assert payload["after"] == {"user_activity_retention_months": 6}
    with pytest.raises(ValidationError) as info:
        await service.set_retention(admin)
    assert info.value.code == svc.INVALID_RETENTION


async def test_guard_error_rolls_back(
    session: AsyncSession, service: SystemSettingsService
) -> None:
    admin = await make_user(session, superuser=True)
    before = await current(session)
    await session.execute(
        text("ALTER TABLE partman.part_config DISABLE TRIGGER guard_audit_part_config")
    )
    await session.execute(
        text(
            "UPDATE partman.part_config SET retention_keep_table = false "
            "WHERE parent_table = 'public.audit_logs'"
        )
    )
    await session.execute(
        text("ALTER TABLE partman.part_config ENABLE TRIGGER guard_audit_part_config")
    )
    await session.commit()
    with pytest.raises(DomainError) as info:
        await service.set_retention(admin, 7, 48)
    assert info.value.code == svc.RETENTION_GUARD
    assert "retention_keep_table" in str(info.value)
    assert await current(session) == before
    assert await audit_count(session, "system.settings.retention", admin) == 0


async def test_maintenance_visible_to_gate(
    session: AsyncSession, service: SystemSettingsService, gate: MaintenanceGate
) -> None:
    admin = await make_user(session, superuser=True)
    assert await gate.is_on() is False
    await service.set_maintenance(admin, True)
    assert await gate.is_on() is True  # cache reset after commit (ttl=3600 otherwise)
    assert await audit_count(session, "system.settings.maintenance", admin) == 1
    await service.set_maintenance(admin, False)
    assert await gate.is_on() is False


async def test_ttl_and_file_logs(session: AsyncSession, service: SystemSettingsService) -> None:
    admin = await make_user(session, superuser=True)
    await service.set_iam_cache_ttl(admin, 600)
    assert service.cache_invalidated is True
    await service.set_file_logs_retention_days(admin, 30)
    assert (await current(session))[2:] == (30, 600)
    assert await audit_count(session, "system.settings.iam_cache_ttl", admin) == 1
    assert await audit_count(session, "system.settings.file_logs_retention", admin) == 1


async def test_denied_without_settings_manage(
    session: AsyncSession, service: SystemSettingsService
) -> None:
    moderator = await make_user(session, role=ROLE_MODERATOR)
    before = await current(session)
    with pytest.raises(PermissionDeniedError) as info:
        await service.set_retention(moderator, 4, 30)
    assert info.value.code == PERMISSION_DENIED
    with pytest.raises(PermissionDeniedError):
        await service.set_maintenance(moderator, True)
    with pytest.raises(PermissionDeniedError):
        await service.set_iam_cache_ttl(moderator, 600)
    with pytest.raises(PermissionDeniedError):
        await service.set_file_logs_retention_days(moderator, 30)
    assert await current(session) == before
    for action in ("retention", "maintenance", "iam_cache_ttl", "file_logs_retention"):
        assert await audit_count(session, f"system.settings.{action}", moderator) == 0


@pytest.mark.parametrize(
    ("method", "args", "code"),
    [
        ("set_retention", (0, 24), svc.INVALID_RETENTION),
        ("set_retention", (3, 121), svc.INVALID_RETENTION),
        ("set_file_logs_retention_days", (0,), svc.INVALID_FILE_LOGS_RETENTION),
        ("set_file_logs_retention_days", (3651,), svc.INVALID_FILE_LOGS_RETENTION),
        ("set_iam_cache_ttl", (0,), svc.INVALID_IAM_CACHE_TTL),
        ("set_iam_cache_ttl", (2_592_001,), svc.INVALID_IAM_CACHE_TTL),
    ],
)
async def test_range_rejected(
    session: AsyncSession, service: SystemSettingsService, method: str, args: Any, code: str
) -> None:
    admin = await make_user(session, superuser=True)
    with pytest.raises(ValidationError) as info:
        await getattr(service, method)(admin, *args)
    assert info.value.code == code


async def test_range_bounds_accepted(session: AsyncSession, service: SystemSettingsService) -> None:
    admin = await make_user(session, superuser=True)
    await service.set_retention(admin, 1, 120)
    assert await retention_synced(session, 1, 120)
    await service.set_file_logs_retention_days(admin, 3650)
    await service.set_file_logs_retention_days(admin, 1)
    assert (await current(session))[:3] == (1, 120, 1)
    await service.set_iam_cache_ttl(admin, 2_592_000)
    assert (await current(session))[3] == 2_592_000
    await service.set_iam_cache_ttl(admin, 1)
    assert (await current(session))[3] == 1


async def _break_audit_config(session: AsyncSession) -> None:
    await session.execute(
        text("ALTER TABLE partman.part_config DISABLE TRIGGER guard_audit_part_config")
    )
    await session.execute(
        text(
            "UPDATE partman.part_config SET retention_keep_table = false "
            "WHERE parent_table = 'public.audit_logs'"
        )
    )
    await session.execute(
        text("ALTER TABLE partman.part_config ENABLE TRIGGER guard_audit_part_config")
    )
    await session.commit()


async def test_db_trigger_guard_rolls_back(
    session: AsyncSession, service: SystemSettingsService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python guard bypassed: the ``guard_audit_part_config`` trigger is the last line."""
    admin = await make_user(session, superuser=True)
    before = await current(session)
    await _break_audit_config(session)
    monkeypatch.setattr(retention, "check_audit_guard", lambda row: None)
    with pytest.raises(DomainError) as info:
        await service.set_retention(admin, 7, 48)
    assert info.value.code == svc.RETENTION_GUARD
    assert await current(session) == before
    assert await retention_synced(session, before[0], before[1])
    assert await audit_count(session, "system.settings.retention", admin) == 0


async def test_config_error_is_not_reported_as_guard(
    session: AsyncSession, service: SystemSettingsService, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await make_user(session, superuser=True)
    before = await current(session)

    def missing(*_args: Any) -> Any:
        raise retention.PartConfigMissingError("partman.part_config has no row")

    monkeypatch.setattr(retention, "plan_changes", missing)
    with pytest.raises(DomainError) as info:
        await service.set_retention(admin, 7, 48)
    assert info.value.code == svc.RETENTION_CONFIG_ERROR
    assert await current(session) == before
    assert await audit_count(session, "system.settings.retention", admin) == 0


async def test_locked_settings_row_reports_busy(
    session: AsyncSession, redis_client: Any, gate: MaintenanceGate, engine: AsyncEngine
) -> None:
    admin = await make_user(session, superuser=True)
    service = SystemSettingsService(
        session,
        PermissionService(session, PermissionCache(redis_client)),
        gate=gate,
        lock_timeout_ms=200,
    )
    async with engine.connect() as other:
        await other.execute(text("SELECT id FROM system_settings WHERE id = 1 FOR UPDATE"))
        with pytest.raises(DomainError) as info:
            await service.set_maintenance(admin, True)
        await other.rollback()
    assert info.value.code == svc.SETTINGS_BUSY
    assert await audit_count(session, "system.settings.maintenance", admin) == 0


async def test_ttl_cache_invalidation_failure_reported(
    session: AsyncSession, redis_client: Any, gate: MaintenanceGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await make_user(session, superuser=True)
    permissions = PermissionService(session, PermissionCache(redis_client))

    async def unconfirmed() -> Any:
        return SimpleNamespace(ok=False)

    monkeypatch.setattr(permissions, "invalidate_all", unconfirmed)
    failures: list[dict[str, Any]] = []
    counter = SimpleNamespace(inc=lambda **labels: failures.append(labels))
    service = SystemSettingsService(
        session, permissions, gate=gate, invalidation_failures=cast(Any, counter)
    )
    await service.set_iam_cache_ttl(admin, 1200)
    assert service.cache_invalidated is False
    assert failures == [{"scope": "all"}]
    assert (await current(session))[3] == 1200
    assert await audit_count(session, "system.settings.iam_cache_ttl", admin) == 1


async def test_maintenance_noop_keeps_gate_cache(
    session: AsyncSession,
    service: SystemSettingsService,
    gate: MaintenanceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = await make_user(session, superuser=True)
    calls: list[int] = []
    monkeypatch.setattr(gate, "invalidate", lambda: calls.append(1))
    await service.set_maintenance(admin, False)
    assert service._noop is True
    assert calls == []
    assert await audit_count(session, "system.settings.maintenance", admin) == 0
