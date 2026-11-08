"""``SystemSettingsRepository`` statements compiled for PostgreSQL (no database)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import ClauseElement
from sqlalchemy.dialects import postgresql

from app.modules.system.repository import SYSTEM_SETTINGS_MISSING, SystemSettingsRepository
from app.shared_kernel.domain.exceptions import EntityNotFoundError


def _session(result: Any) -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    return session


def _sql(session: MagicMock) -> str:
    stmt: ClauseElement = session.execute.await_args.args[0]
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    return str(stmt.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize(("returned", "expected"), [(1, True), (None, False)])
async def test_ensure_exists_on_conflict_do_nothing(returned: int | None, expected: bool) -> None:
    session = _session(MagicMock(scalar_one_or_none=MagicMock(return_value=returned)))
    assert await SystemSettingsRepository(session).ensure_exists() is expected
    sql = _sql(session)
    assert sql.startswith("INSERT INTO system_settings (id")
    assert "VALUES (1" in sql
    head, _, returning = sql.partition(" RETURNING ")
    assert head.rstrip().endswith("ON CONFLICT (id) DO NOTHING")
    assert returning.strip() == "system_settings.id"
    # Other columns get their Python-side ``default=`` (mirrors of the server defaults);
    # the timestamps have server defaults only and are never sent.
    assert "created_at" not in head
    assert "updated_at" not in head
    session.commit.assert_not_called()


async def test_get_missing_raises_domain_error() -> None:
    session = _session(MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    with pytest.raises(EntityNotFoundError) as caught:
        await SystemSettingsRepository(session).get()
    assert caught.value.code == SYSTEM_SETTINGS_MISSING == "system_settings_missing"
    sql = _sql(session)
    assert "WHERE system_settings.id = 1" in sql
    assert "FOR UPDATE" not in sql


async def test_get_for_update_locks_row() -> None:
    entity = object()
    session = _session(MagicMock(scalar_one_or_none=MagicMock(return_value=entity)))
    assert await SystemSettingsRepository(session).get_for_update() is entity
    assert _sql(session).rstrip().endswith("FOR UPDATE")
