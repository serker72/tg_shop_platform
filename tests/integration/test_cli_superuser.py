"""``python -m app.entrypoints.cli.superuser`` as a subprocess against the test database.

``clean_db``: the CLI commits in its own process; tables are truncated afterwards.
"""

from __future__ import annotations

import asyncio
import itertools
import subprocess
import sys
import uuid
from typing import Any

import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.modules.iam.models import Permission, RolePermission, User, UserPermissionOverride
from app.modules.iam.permissions import SEED_PERMISSION_SLUGS
from app.modules.iam.superuser import ACTION_GRANT, ACTION_REVOKE
from app.modules.logging.models import AuditLog
from tests.integration._cli import assert_no_password, cli_env, run_cli

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.1")]

_IDS = itertools.count(3_000_000_000 + uuid.uuid4().int % 10**8)
_MODULE = "app.entrypoints.cli.superuser"


def tg_id() -> int:
    return next(_IDS)


async def superuser_cli(settings: Settings, *args: str) -> subprocess.CompletedProcess[str]:
    env = cli_env(settings, f"postgresql://cli@localhost/{settings.postgres.db}")
    proc = await asyncio.to_thread(run_cli, [sys.executable, "-m", _MODULE, *args], env)
    assert settings.postgres.password is not None
    assert_no_password(proc.stdout + proc.stderr, settings.postgres.password.get_secret_value())
    return proc


async def user_rows(engine: AsyncEngine, telegram_user_id: int) -> list[Any]:
    stmt = select(
        User.is_superuser, User.is_active, User.is_deleted, User.telegram_user_name
    ).where(User.telegram_user_id == telegram_user_id)
    async with engine.connect() as conn:
        return list((await conn.execute(stmt)).all())


async def audit_count(engine: AsyncEngine, action: str, telegram_user_id: int) -> int:
    user_id = select(User.id).where(User.telegram_user_id == telegram_user_id).scalar_subquery()
    stmt = (
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.action == action,
            AuditLog.actor_type == "system",
            AuditLog.entity_id == user_id,
        )
    )
    async with engine.connect() as conn:
        return int((await conn.scalar(stmt)) or 0)


async def test_create_is_idempotent_and_audited(clean_db: AsyncEngine, settings: Settings) -> None:
    tid = tg_id()
    proc = await superuser_cli(settings, "create", "--telegram-id", str(tid), "--username", "@boss")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert await user_rows(clean_db, tid) == [(True, True, False, "boss")]
    assert await audit_count(clean_db, ACTION_GRANT, tid) == 1

    again = await superuser_cli(settings, "create", "--telegram-id", str(tid))
    assert again.returncode == 0, again.stdout + again.stderr
    assert await user_rows(clean_db, tid) == [(True, True, False, "boss")]
    assert await audit_count(clean_db, ACTION_GRANT, tid) == 1


async def test_create_flags_existing_user(clean_db: AsyncEngine, settings: Settings) -> None:
    tid = tg_id()
    async with clean_db.begin() as conn:
        await conn.execute(insert(User).values(telegram_user_id=tid, is_superuser=False))
    proc = await superuser_cli(settings, "create", "--telegram-id", str(tid))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert await user_rows(clean_db, tid) == [(True, True, False, None)]
    assert await audit_count(clean_db, ACTION_GRANT, tid) == 1


async def test_create_restores_soft_deleted(clean_db: AsyncEngine, settings: Settings) -> None:
    tid = tg_id()
    async with clean_db.begin() as conn:
        await conn.execute(
            insert(User).values(
                telegram_user_id=tid, is_superuser=False, is_active=False, is_deleted=True
            )
        )
    proc = await superuser_cli(settings, "create", "--telegram-id", str(tid))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert await user_rows(clean_db, tid) == [(True, True, False, None)]
    assert await audit_count(clean_db, ACTION_GRANT, tid) == 1


async def test_revoke_keeps_last_superuser_unless_forced(
    clean_db: AsyncEngine, settings: Settings
) -> None:
    first, second = tg_id(), tg_id()
    for tid in (first, second):
        assert (await superuser_cli(settings, "create", "--telegram-id", str(tid))).returncode == 0

    proc = await superuser_cli(settings, "revoke", "--telegram-id", str(first))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (await user_rows(clean_db, first))[0][0] is False
    assert await audit_count(clean_db, ACTION_REVOKE, first) == 1

    refused = await superuser_cli(settings, "revoke", "--telegram-id", str(second))
    assert refused.returncode == 1
    assert "last_superuser" in refused.stdout + refused.stderr
    assert (await user_rows(clean_db, second))[0][0] is True
    assert await audit_count(clean_db, ACTION_REVOKE, second) == 0

    forced = await superuser_cli(settings, "revoke", "--telegram-id", str(second), "--force")
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert (await user_rows(clean_db, second))[0][0] is False
    assert await audit_count(clean_db, ACTION_REVOKE, second) == 1

    noop = await superuser_cli(settings, "revoke", "--telegram-id", str(second))
    assert noop.returncode == 0
    assert await audit_count(clean_db, ACTION_REVOKE, second) == 1


async def test_revoke_unknown_user_fails(clean_db: AsyncEngine, settings: Settings) -> None:
    proc = await superuser_cli(settings, "revoke", "--telegram-id", str(tg_id()))
    assert proc.returncode == 1
    assert "user_not_found" in proc.stdout + proc.stderr


async def test_missing_seed_permissions_fail(clean_db: AsyncEngine, settings: Settings) -> None:
    slug = sorted(SEED_PERMISSION_SLUGS)[0]
    perm_ids = select(Permission.id).where(Permission.slug == slug)
    async with clean_db.begin() as conn:
        await conn.execute(delete(RolePermission).where(RolePermission.permission_id.in_(perm_ids)))
        await conn.execute(
            delete(UserPermissionOverride).where(UserPermissionOverride.permission_id.in_(perm_ids))
        )
        await conn.execute(delete(Permission).where(Permission.slug == slug))
    tid = tg_id()
    proc = await superuser_cli(settings, "create", "--telegram-id", str(tid))
    assert proc.returncode == 1
    assert "apply migrations" in proc.stdout + proc.stderr
    assert await user_rows(clean_db, tid) == []


async def test_bad_arguments_exit_2(settings: Settings, migrated_db: Any) -> None:
    proc = await superuser_cli(settings, "create", "--telegram-id", "0")
    assert proc.returncode == 2
