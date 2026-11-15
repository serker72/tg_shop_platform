"""IAM admin services with real commits on two connections (``clean_db``, no ``session``)
and the constraint-name contract of :func:`map_integrity_error`."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.modules.iam import admin_service as svc
from app.modules.iam.cache import PermissionCache
from app.modules.iam.models import User
from app.modules.iam.service import PermissionService
from app.modules.logging.models import AuditLog

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.2")]

TG = 3_000_000_001


async def test_update_username_sees_row_changed_by_other_connection(
    clean_db: AsyncEngine, redis_client: Any
) -> None:
    """R1: the locked re-read must refresh the identity-map object (``populate_existing``)."""
    async with AsyncSession(clean_db, expire_on_commit=False) as session:
        session.add(User(telegram_user_id=TG, telegram_user_name="old"))
        await session.commit()
        stale = (await session.scalars(select(User).where(User.telegram_user_id == TG))).one()
        await session.commit()
        async with clean_db.begin() as other:
            await other.execute(
                text("UPDATE users SET telegram_user_name = 'other' WHERE telegram_user_id = :tg"),
                {"tg": TG},
            )
        service = svc.UserAdminService(
            session, PermissionService(session, PermissionCache(redis_client))
        )
        user = await service.update_username(TG, "new")
        assert user is stale and user.telegram_user_name == "new"
        rows = (
            await session.scalars(
                select(AuditLog).where(AuditLog.action == "iam.user.update_username")
            )
        ).all()
        assert len(rows) == 1 and rows[0].payload is not None
        assert rows[0].payload["before"]["telegram_user_name"] == "other"

        # Same value already stored by another connection -> no write, no audit.
        async with clean_db.begin() as other:
            await other.execute(
                text("UPDATE users SET telegram_user_name = 'third' WHERE telegram_user_id = :tg"),
                {"tg": TG},
            )
        await service.update_username(TG, "third")
        count = len(
            (
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == "iam.user.update_username")
                )
            ).all()
        )
        assert count == 1


async def _constraints(engine: AsyncEngine, contype: str) -> list[tuple[str, str]]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT conname, confrelid::regclass::text FROM pg_constraint "
                "WHERE contype = :t AND connamespace = 'public'::regnamespace"
            ),
            {"t": contype},
        )
        return [(row[0], row[1]) for row in result]


async def test_unique_codes_match_database_constraints(engine: AsyncEngine) -> None:
    """R4: every mapped unique constraint exists under exactly this name."""
    names = {name for name, _ in await _constraints(engine, "u")}
    missing = set(svc._UNIQUE_CODES) - names
    assert not missing, missing
    from app.modules.iam.admin_repository import OVERRIDE_SLOT_CONSTRAINT

    assert OVERRIDE_SLOT_CONSTRAINT in names


async def test_fk_suffix_mapping_matches_database_constraints(engine: AsyncEngine) -> None:
    """R4: FKs to mapped tables end with ``_<table>`` and map to the table's code."""
    checked = 0
    for name, referred in await _constraints(engine, "f"):
        if referred not in svc._FK_CODES:
            continue
        assert name.endswith(f"_{referred}"), name
        orig = type(
            "Orig", (), {"sqlstate": "23503", "diag": type("D", (), {"constraint_name": name})}
        )
        from sqlalchemy.exc import IntegrityError

        error = svc.map_integrity_error(IntegrityError("x", {}, orig()))
        assert error.code == svc._FK_CODES[referred], name
        checked += 1
    assert checked >= len(svc._FK_CODES)
