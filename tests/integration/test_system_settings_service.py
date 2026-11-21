"""``SystemSettingsService`` against real PostgreSQL (savepoint session) and test Redis."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.modules.iam.cache import PermissionCache
from app.modules.iam.models import Role, User, UserRole
from app.modules.iam.permissions import ROLE_MODERATOR
from app.modules.iam.service import PERMISSION_DENIED, PermissionService
from app.modules.logging import retention
from app.modules.logging.models import AuditLog
from app.modules.system import service as svc
from app.modules.system.cache import SystemSettingsCache
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
def settings_cache(
    redis_client: Any, session_factory: async_sessionmaker[AsyncSession]
) -> SystemSettingsCache:
    return SystemSettingsCache(redis_client, session_factory)


@pytest.fixture
def gate(settings_cache: SystemSettingsCache) -> MaintenanceGate:
    return MaintenanceGate(settings_cache)


@pytest.fixture
def service(
    session: AsyncSession, redis_client: Any, settings_cache: SystemSettingsCache
) -> SystemSettingsService:
    return SystemSettingsService(
        session,
        PermissionService(session, PermissionCache(redis_client), settings_cache=settings_cache),
        settings_cache=settings_cache,
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
    with capture_logs() as logs:
        await service.set_retention(admin, 4, 48)  # explicitly supplied UA did not change
        await service.set_retention(admin, 4, 48)  # repeated request is a no-op
    assert [log for log in logs if log["event"] == "system_settings_changed"] == [
        {
            "event": "system_settings_changed",
            "log_level": "info",
            "action": "system.settings.retention",
            "actor_id": admin,
            "before": {"audit_logs_retention_months": 30},
            "after": {"audit_logs_retention_months": 48},
        }
    ]
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
    with capture_logs() as logs, pytest.raises(DomainError) as info:
        await service.set_retention(admin, 7, 48)
    assert not any(log["event"] == "system_settings_changed" for log in logs)
    assert info.value.code == svc.RETENTION_GUARD
    assert "retention_keep_table" in str(info.value)
    assert await current(session) == before
    assert await audit_count(session, "system.settings.retention", admin) == 0


async def test_maintenance_visible_to_gate(
    session: AsyncSession,
    service: SystemSettingsService,
    gate: MaintenanceGate,
    redis_client: Any,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = await make_user(session, superuser=True)
    # Another process: its own cache/gate instances over the same Redis.
    other = MaintenanceGate(SystemSettingsCache(redis_client, session_factory))
    assert await gate.is_on() is False
    assert await other.is_on() is False
    assert await redis_client.exists("settings:snapshot") == 1  # warmed by the read
    await service.set_maintenance(admin, True)
    assert service.cache_invalidated is True
    assert await other.is_on() is True
    assert await gate.is_on() is True
    assert await audit_count(session, "system.settings.maintenance", admin) == 1
    await service.set_maintenance(admin, False)
    assert await other.is_on() is False


async def test_permission_ttl_taken_from_settings_cache(
    session: AsyncSession,
    service: SystemSettingsService,
    redis_client: Any,
    settings_cache: SystemSettingsCache,
) -> None:
    admin = await make_user(session, superuser=True)
    await service.set_iam_cache_ttl(admin, 777)
    user = await make_user(session, role=ROLE_MODERATOR)
    permissions = PermissionService(
        session, PermissionCache(redis_client), settings_cache=settings_cache
    )
    await permissions.get_snapshot(user)
    ttl = await redis_client.ttl(PermissionCache.key(user))
    assert 0 < ttl <= 777
    assert (await settings_cache.get()).iam_cache_ttl_seconds == 777


async def test_redis_down_service_works_but_reports_stale_cache(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], settings: Any
) -> None:
    from app.shared_kernel.infrastructure.redis import build_redis

    admin = await make_user(session, superuser=True)
    dead_settings = settings.model_copy(
        update={"redis": settings.redis.model_copy(update={"port": 1})}
    )
    dead = build_redis(dead_settings)
    try:
        cache = SystemSettingsCache(dead, session_factory)
        failures: list[dict[str, Any]] = []
        counter = SimpleNamespace(inc=lambda **labels: failures.append(labels))
        service = SystemSettingsService(
            session,
            PermissionService(session, PermissionCache(dead), settings_cache=cache),
            settings_cache=cache,
            invalidation_failures=cast(Any, counter),
        )
        await service.set_maintenance(admin, True)
        assert service.cache_invalidated is False
        assert failures == [{"scope": "settings"}]
        assert await MaintenanceGate(cache).is_on() is True  # Redis down -> PostgreSQL
    finally:
        await dead.aclose()


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
    session: AsyncSession,
    redis_client: Any,
    settings_cache: SystemSettingsCache,
    engine: AsyncEngine,
) -> None:
    admin = await make_user(session, superuser=True)
    service = SystemSettingsService(
        session,
        PermissionService(session, PermissionCache(redis_client)),
        settings_cache=settings_cache,
        lock_timeout_ms=200,
    )
    async with engine.connect() as other:
        await other.execute(text("SELECT id FROM system_settings WHERE id = 1 FOR UPDATE"))
        with pytest.raises(DomainError) as info:
            # wait_for: without ``SET LOCAL lock_timeout`` the test fails instead of hanging.
            await asyncio.wait_for(service.set_maintenance(admin, True), timeout=10)
        await other.rollback()
    assert info.value.code == svc.SETTINGS_BUSY
    assert await audit_count(session, "system.settings.maintenance", admin) == 0


async def test_ttl_cache_invalidation_failure_reported(
    session: AsyncSession,
    redis_client: Any,
    settings_cache: SystemSettingsCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = await make_user(session, superuser=True)
    permissions = PermissionService(session, PermissionCache(redis_client))

    async def unconfirmed() -> Any:
        return SimpleNamespace(ok=False)

    monkeypatch.setattr(permissions, "invalidate_all", unconfirmed)
    failures: list[dict[str, Any]] = []
    counter = SimpleNamespace(inc=lambda **labels: failures.append(labels))
    service = SystemSettingsService(
        session,
        permissions,
        settings_cache=settings_cache,
        invalidation_failures=cast(Any, counter),
    )
    await service.set_iam_cache_ttl(admin, 1200)
    assert service.cache_invalidated is False
    assert failures == [{"scope": "all"}]
    assert (await current(session))[3] == 1200
    assert await audit_count(session, "system.settings.iam_cache_ttl", admin) == 1


async def test_maintenance_noop_keeps_settings_cache(
    session: AsyncSession,
    service: SystemSettingsService,
    settings_cache: SystemSettingsCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = await make_user(session, superuser=True)
    calls: list[int] = []

    async def invalidate() -> Any:
        calls.append(1)
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(settings_cache, "invalidate", invalidate)
    with capture_logs() as logs:
        await service.set_maintenance(admin, False)
    assert not any(log["event"] == "system_settings_changed" for log in logs)
    assert service._noop is True
    assert calls == []
    assert await audit_count(session, "system.settings.maintenance", admin) == 0


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_settings_log_only_after_commit(
    session: AsyncSession,
    service: SystemSettingsService,
    monkeypatch: pytest.MonkeyPatch,
    commit_fails: bool,
) -> None:
    actor = await make_user(session, superuser=True)
    commit = session.commit
    action = "system.settings.maintenance"

    with capture_logs() as logs:

        async def checked_commit() -> None:
            assert not any(log["event"] == "system_settings_changed" for log in logs)
            if commit_fails:
                raise RuntimeError("commit failed")
            await commit()
            assert not any(log["event"] == "system_settings_changed" for log in logs)

        monkeypatch.setattr(session, "commit", checked_commit)
        if commit_fails:
            with pytest.raises(RuntimeError, match="commit failed"):
                await service.set_maintenance(actor, True)
        else:
            await service.set_maintenance(actor, True)

    changes = [log for log in logs if log["event"] == "system_settings_changed"]
    if commit_fails:
        assert changes == []
        assert (await service.get()).is_maintenance_mode is False
        assert await audit_count(session, action, actor) == 0
    else:
        assert changes == [
            {
                "event": "system_settings_changed",
                "log_level": "info",
                "action": action,
                "actor_id": actor,
                "before": {"is_maintenance_mode": False},
                "after": {"is_maintenance_mode": True},
            }
        ]
        assert (await service.get()).is_maintenance_mode is True
        assert await audit_count(session, action, actor) == 1


@pytest.fixture
def maintenance_metric_sql() -> str:
    path = Path(__file__).resolve().parents[2] / "ops/observability/alloy/postgres_queries.yml"
    query = yaml.safe_load(path.read_text(encoding="utf-8"))["system"]["query"]
    assert isinstance(query, str)
    return query


async def test_maintenance_metric_queries_postgres_without_fabricating_zero(
    session: AsyncSession, maintenance_metric_sql: str
) -> None:
    query = text(maintenance_metric_sql)
    for enabled, expected in [(False, 0), (True, 1)]:
        await session.execute(update(SystemSettings).values(is_maintenance_mode=enabled))
        rows = (await session.execute(query)).mappings().all()
        assert [dict(row) for row in rows] == [{"maintenance_mode": expected}]

    # The integration fixture rolls back the outer transaction, including this deletion.
    await session.execute(delete(SystemSettings))
    assert (await session.execute(query)).all() == []
