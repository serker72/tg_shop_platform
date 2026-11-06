"""Tests for :class:`app.shared_kernel.infrastructure.base_repository.BaseRepository`.

Statements are compiled for the PostgreSQL dialect, so no database connection
is required. The session is replaced by a tiny async double that records the
SQL passed to ``scalars()`` plus ``add()`` / ``flush()`` calls.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import pytest
from sqlalchemy import MetaData, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, make_transient_to_detached, mapped_column

from app.shared_kernel.domain.base_entity import BusinessEntity
from app.shared_kernel.domain.exceptions import EntityNotFoundError
from app.shared_kernel.infrastructure.base_repository import BaseRepository
from app.shared_kernel.infrastructure.database import NAMING_CONVENTION

DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]  # untyped factory stub


class _TestBase(DeclarativeBase):
    """Isolated declarative root: ``metadata`` is a class variable, not per instance."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class _Widget(_TestBase, BusinessEntity):
    __tablename__ = "widget"

    name: Mapped[str] = mapped_column()


class _ResultDouble:
    def first(self) -> None:
        return None

    def all(self) -> list[Any]:
        return []


class _SessionDouble:
    """Minimal stand-in for ``AsyncSession`` recording executed statements."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.added: list[object] = []
        self.flushed = 0

    async def scalars(self, stmt: Any) -> _ResultDouble:  # noqa: ANN401 - SQL statement object
        self.statements.append(str(stmt.compile(dialect=DIALECT)))
        return _ResultDouble()

    def add(self, entity: object) -> None:
        self.added.append(entity)

    async def flush(self) -> None:
        self.flushed += 1


class _Compilable(Protocol):
    """A SQL constructable that can be rendered as PostgreSQL SQL."""

    def compile(self, *, dialect: Any) -> Any: ...  # noqa: ANN401 - stub-free dialect object


def _sql(stmt: _Compilable) -> str:
    return str(stmt.compile(dialect=DIALECT))


def _has_where(sql: str) -> bool:
    """Return ``True`` when the statement carries a WHERE clause."""
    return " WHERE " in sql


@pytest.fixture()
def skip_managed_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass ``_ensure_managed`` for tests that use the session double.

    The double is not a real session, so an entity can never be persistent in it;
    the guard itself is covered by the tests on a real ``AsyncSession`` below.
    """
    monkeypatch.setattr(BaseRepository, "_ensure_managed", lambda self, entity: None)


# Never connected: identity-map bookkeeping (add/inspect) needs no database.
_OFFLINE_URL = "postgresql+psycopg://u:p@localhost:1/x"


def _real_repo() -> tuple[BaseRepository[_Widget], AsyncSession]:
    session = AsyncSession(create_async_engine(_OFFLINE_URL))
    return BaseRepository(session=session, model=_Widget), session


def _persistent_in(session: AsyncSession, *, is_deleted: bool = False) -> _Widget:
    """Make a persistent object without SQL: detached state + ``add`` -> persistent."""
    widget = _new_widget()
    widget.id = uuid.uuid4()
    widget.is_deleted = is_deleted
    widget.is_active = True
    # Fully loaded state: no attribute is expired before the repository acts.
    widget.created_at = widget.updated_at = datetime.now(UTC)
    make_transient_to_detached(widget)
    session.add(widget)
    return widget


def _repo() -> tuple[BaseRepository[_Widget], _SessionDouble]:
    session = _SessionDouble()
    # The double implements only the subset of AsyncSession the repository uses.
    return BaseRepository(session=cast("AsyncSession", session), model=_Widget), session


def _new_widget() -> _Widget:
    widget = _Widget()
    widget.name = "w"
    return widget


def test_base_stmt_selects_all_columns_without_alive_filter() -> None:
    repo, _ = _repo()
    text = _sql(repo._base_stmt())
    assert text.startswith("SELECT widget.")
    assert "FROM widget" in text
    assert _has_where(text) is False


def test_apply_alive_filter_adds_is_deleted_false() -> None:
    repo, _ = _repo()
    alive = repo._apply_alive_filter(repo._base_stmt())
    assert "widget.is_deleted IS false" in _sql(alive)


async def test_get_by_id_filters_soft_deleted() -> None:
    repo, session = _repo()
    assert await repo.get_by_id(uuid.uuid4()) is None
    assert "widget.is_deleted IS false" in session.statements[-1]


async def test_get_by_id_include_deleted_skips_filter() -> None:
    repo, session = _repo()
    await repo.get_by_id(uuid.uuid4(), include_deleted=True)
    assert _has_where(session.statements[-1]) is False


async def test_list_applies_alive_filter_limit_offset_and_order() -> None:
    repo, session = _repo()
    await repo.list(limit=5, offset=10)
    sql = session.statements[-1]
    assert "widget.is_deleted IS false" in sql
    assert "ORDER BY widget.created_at, widget.id" in sql
    assert "LIMIT" in sql
    assert "OFFSET" in sql


async def test_list_include_deleted_skips_filter() -> None:
    repo, session = _repo()
    await repo.list(include_deleted=True)
    assert _has_where(session.statements[-1]) is False


async def test_create_adds_and_flushes_without_commit() -> None:
    repo, session = _repo()
    entity = _new_widget()

    assert await repo.create(entity) is entity
    assert session.added == [entity]
    assert session.flushed == 1


def _count_flushes(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace ``flush`` on a real (never connected) session with a counter."""
    calls: list[int] = []

    async def _flush() -> None:
        calls.append(1)

    monkeypatch.setattr(session, "flush", _flush)
    return calls


async def test_update_flushes_and_returns_same_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    repo, session = _real_repo()
    flushes = _count_flushes(session, monkeypatch)
    entity = _persistent_in(session)
    entity.name = "renamed"

    assert await repo.update(entity) is entity
    assert flushes == [1]


async def test_update_of_tombstone_discards_pending_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, session = _real_repo()
    flushes = _count_flushes(session, monkeypatch)
    entity = _persistent_in(session, is_deleted=True)
    entity.name = "changed"

    with pytest.raises(EntityNotFoundError) as excinfo:
        await repo.update(entity)

    assert excinfo.value.code == "entity_deleted"
    assert flushes == []
    state = inspect(entity)
    # Expired: the dirty value is gone and will not be written by a later autoflush;
    # untouched ``is_deleted`` and the primary key stay loaded.
    assert state.expired_attributes == {"name"}
    # A partial expire keeps the object in ``session.dirty`` (the modified flag is
    # coarse), but it has no net attribute changes, so the flush emits no UPDATE.
    assert not session.is_modified(entity)


def _assert_only_changed_expired(entity: _Widget, changed: set[str]) -> None:
    """After a refusal: PK and untouched fields readable, changed fields expired.

    The session's engine points to an unreachable port, so reading an expired
    attribute would fail; plain reads below prove they are served from memory.
    """
    expired = inspect(entity).expired_attributes
    assert expired == changed
    assert "id" not in expired
    assert isinstance(entity.id, uuid.UUID)
    assert entity.is_active is True


@pytest.mark.parametrize(
    ("stored_deleted", "changes", "code"),
    [
        (True, {"name": "changed"}, "entity_deleted"),
        (True, {}, "entity_deleted"),
        (False, {"is_deleted": True}, "use_soft_delete"),
        (False, {"is_deleted": True, "name": "changed"}, "use_soft_delete"),
    ],
)
async def test_refused_update_expires_only_changed_attributes(
    stored_deleted: bool, changes: dict[str, object], code: str
) -> None:
    repo, session = _real_repo()
    entity = _persistent_in(session, is_deleted=stored_deleted)
    for key, value in changes.items():
        setattr(entity, key, value)

    with pytest.raises(EntityNotFoundError) as excinfo:
        await repo.update(entity)

    assert excinfo.value.code == code
    # Nothing dirty -> fallback expires ``is_deleted`` only.
    _assert_only_changed_expired(entity, set(changes) or {"is_deleted"})
    if "name" not in changes:
        assert entity.name == "w"


async def test_update_cannot_restore_by_flipping_flag() -> None:
    repo, session = _real_repo()
    entity = _persistent_in(session, is_deleted=True)
    entity.is_deleted = False

    with pytest.raises(EntityNotFoundError) as excinfo:
        await repo.update(entity)
    assert excinfo.value.code == "entity_deleted"
    assert "is_deleted" in inspect(entity).expired_attributes


async def test_update_cannot_soft_delete_by_flipping_flag() -> None:
    repo, session = _real_repo()
    entity = _persistent_in(session)
    entity.is_deleted = True

    with pytest.raises(EntityNotFoundError) as excinfo:
        await repo.update(entity)
    assert excinfo.value.code == "use_soft_delete"
    assert "is_deleted" in inspect(entity).expired_attributes


async def test_update_with_expired_flag_fails_without_io() -> None:
    # The engine points to an unreachable port: any lazy load would raise a driver
    # error (or MissingGreenlet), so getting the domain error proves no IO happened.
    repo, session = _real_repo()
    entity = _persistent_in(session)
    session.expire(entity, ["is_deleted"])

    with pytest.raises(EntityNotFoundError) as excinfo:
        await repo.update(entity)
    assert excinfo.value.code == "entity_state_unknown"
    assert "expire_on_commit=False" in str(excinfo.value)


@pytest.mark.usefixtures("skip_managed_check")
async def test_soft_delete_is_idempotent() -> None:
    repo, session = _repo()
    entity = _new_widget()

    await repo.soft_delete(entity)
    await repo.soft_delete(entity)

    assert entity.is_deleted is True
    assert session.flushed == 2


async def test_transient_entity_is_rejected() -> None:
    repo, _ = _real_repo()
    entity = _new_widget()

    for operation in (repo.update, repo.soft_delete, repo.restore):
        with pytest.raises(EntityNotFoundError) as excinfo:
            await operation(entity)
        assert excinfo.value.code == "entity_not_managed"
    assert entity.is_deleted is None


async def test_entity_from_another_session_is_rejected() -> None:
    repo, _ = _real_repo()
    _, other_session = _real_repo()
    entity = _persistent_in(other_session)

    with pytest.raises(EntityNotFoundError) as excinfo:
        await repo.soft_delete(entity)
    assert excinfo.value.code == "entity_not_managed"
    assert entity.is_deleted is False


async def test_update_of_soft_deleted_entity_is_rejected() -> None:
    # Real persistent state in the repository's own session: the guard passes and
    # the tombstone check fires before any flush (so no database is touched).
    repo, session = _real_repo()
    entity = _persistent_in(session, is_deleted=True)

    with pytest.raises(EntityNotFoundError) as excinfo:
        await repo.update(entity)
    assert excinfo.value.code == "entity_deleted"


@pytest.mark.usefixtures("skip_managed_check")
async def test_soft_delete_sets_both_flags() -> None:
    repo, session = _repo()
    entity = _new_widget()
    entity.is_deleted = False
    entity.is_active = True

    await repo.soft_delete(entity)

    assert entity.is_deleted is True
    assert entity.is_active is False
    assert session.flushed == 1


@pytest.mark.usefixtures("skip_managed_check")
async def test_restore_clears_only_is_deleted() -> None:
    repo, session = _repo()
    entity = _new_widget()
    entity.is_deleted = True
    entity.is_active = False

    await repo.restore(entity)

    assert entity.is_deleted is False
    assert entity.is_active is False
    assert session.flushed == 1


def test_repository_has_no_physical_delete() -> None:
    public_api = {name for name in dir(BaseRepository) if not name.startswith("_")}
    assert "delete" not in public_api
    assert "hard_delete" not in public_api
    with pytest.raises(AttributeError):
        _ = BaseRepository.delete  # type: ignore[attr-defined]
