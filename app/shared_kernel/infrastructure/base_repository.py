"""Generic async repository over :class:`sqlalchemy.ext.asyncio.AsyncSession`.

Design rules:

* the repository never calls ``commit()`` - transaction boundaries belong to
  the unit-of-work / request handler, so several repositories can participate
  in one atomic operation;
* physical ``DELETE`` is forbidden: business entities are only soft-deleted,
  therefore no ``delete()`` method is provided on purpose;
* every read method filters out tombstones (``is_deleted IS false``) unless the
  caller explicitly opts in with ``include_deleted=True``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.base import NO_VALUE

from app.shared_kernel.domain.base_entity import BusinessEntity
from app.shared_kernel.domain.exceptions import EntityNotFoundError

__all__ = ["BaseRepository"]


class BaseRepository[T: BusinessEntity]:
    """CRUD facade for a single soft-deletable entity type.

    Attributes:
        _session: session owned by the caller; never closed or committed here.
        _model: mapped class served by this repository.

    Note: intentionally no ``delete()`` method - a hard ``DELETE`` is forbidden
    project-wide. Use :meth:`soft_delete` instead; physical purge (if it is ever
    required) belongs to an explicit administrative maintenance script, not to
    regular application code.
    """

    def __init__(self, session: AsyncSession, model: type[T]) -> None:
        self._session = session
        self._model = model

    # ------------------------------------------------------------------ reads

    def _base_stmt(self) -> Select[T]:
        """Return a ``SELECT`` for the model already narrowed by the alive filter.

        The global soft-delete filter is applied here, so every read method
        inherits it.
        """
        return select(self._model).where(self._model.is_deleted.is_(False))

    def _apply_alive_filter(self, stmt: Select[T]) -> Select[T]:
        """Add the global ``is_deleted IS false`` filter to an arbitrary select."""
        return stmt.where(self._model.is_deleted.is_(False))

    async def get_by_id(self, entity_id: uuid.UUID, *, include_deleted: bool = False) -> T | None:
        """Return the entity by primary key or ``None`` when it does not exist.

        Args:
            entity_id: primary key value.
            include_deleted: also match soft-deleted rows when ``True``.
        """
        if include_deleted:
            stmt = select(self._model).where(self._model.id == entity_id)
        else:
            stmt = self._base_stmt().where(self._model.id == entity_id)
        rows = await self._session.scalars(stmt)
        return rows.first()

    async def list(
        self,
        *,
        include_deleted: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[T]:
        """Return a page of entities ordered by ``created_at`` then ``id``.

        ``id`` is a tie-breaker: rows created in one transaction share ``now()``,
        and without it ``LIMIT/OFFSET`` pagination is not deterministic.

        Args:
            include_deleted: also return soft-deleted rows when ``True``.
            limit: maximum number of rows, ``None`` means no limit.
            offset: number of rows to skip.
        """
        stmt = select(self._model) if include_deleted else self._base_stmt()
        stmt = stmt.order_by(self._model.created_at, self._model.id).offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = await self._session.scalars(stmt)
        return rows.all()

    # ----------------------------------------------------------------- writes

    async def create(self, entity: T) -> T:
        """Persist a new entity (``add`` + ``flush``); the caller commits."""
        self._session.add(entity)
        await self._session.flush()
        return entity

    def _ensure_managed(self, entity: T) -> None:
        """Raise unless ``entity`` is persistent in *this* repository's session.

        Transient, pending, detached or foreign-session objects are rejected: no
        ``merge`` is ever performed, so a stale copy can neither INSERT a new row
        nor resurrect a tombstone.
        """
        state = inspect(entity)
        if not state.persistent or state.session is not self._session.sync_session:
            raise EntityNotFoundError(
                f"{type(entity).__name__} is not loaded in the current session",
                code="entity_not_managed",
            )

    def _check_updatable(self, entity: T) -> None:
        """Validate ``is_deleted`` against the *persisted* value, without IO.

        The persisted value is ``history.deleted[0]`` when the attribute was changed,
        otherwise ``loaded_value``. ``AttributeState.history`` uses
        ``PASSIVE_NO_INITIALIZE``, so an expired attribute yields ``NO_VALUE`` instead
        of a lazy load (which would raise ``MissingGreenlet`` under ``AsyncSession``).
        """
        name = type(entity).__name__
        attr = inspect(entity).attrs.is_deleted
        history = attr.history
        if history.deleted:
            stored: object = history.deleted[0]
        elif history.added:
            stored = NO_VALUE  # changed after expiry: the original value was never loaded
        else:
            stored = attr.loaded_value
        if stored is NO_VALUE:
            raise EntityNotFoundError(
                f"{name}.is_deleted is not loaded; sessions must use expire_on_commit=False",
                code="entity_state_unknown",
            )
        current = history.added[0] if history.added else stored
        if stored:
            raise EntityNotFoundError(f"{name} is soft-deleted", code="entity_deleted")
        if current:
            raise EntityNotFoundError(
                f"{name}.is_deleted must be changed via soft_delete()",
                code="use_soft_delete",
            )

    async def update(self, entity: T) -> T:
        """Flush changes of an entity loaded through this repository / session.

        The entity must be persistent in the repository's session (loaded via
        :meth:`get_by_id` / :meth:`list` or created via :meth:`create`). The check
        uses the *persisted* ``is_deleted`` value, not the in-memory one:

        * persisted tombstone -> ``entity_deleted`` (restore only via :meth:`restore`,
          also when the caller flipped ``is_deleted`` back to ``False`` by hand);
        * live row with ``is_deleted`` set to ``True`` by hand ->
          ``use_soft_delete`` (deletion only via :meth:`soft_delete`);
        * ``is_deleted`` expired (session with ``expire_on_commit=True``) ->
          ``entity_state_unknown``; no lazy load is attempted.

        On refusal only the *changed* attributes are expired (``session.expire`` with an
        attribute list; ``is_deleted`` when nothing is dirty), so the next autoflush
        cannot write them behind the caller's back. The primary key and unchanged
        attributes stay loaded and remain readable without IO (no ``MissingGreenlet``
        in an ``except`` block). Change detection uses ``AttributeState.history``,
        which never emits loader callables for unloaded attributes.

        Raises:
            EntityNotFoundError: ``entity_not_managed``, ``entity_deleted``,
                ``use_soft_delete`` or ``entity_state_unknown``.
        """
        self._ensure_managed(entity)
        try:
            self._check_updatable(entity)
        except EntityNotFoundError:
            changed = [attr.key for attr in inspect(entity).attrs if attr.history.has_changes()]
            self._session.expire(entity, changed or ["is_deleted"])
            raise
        await self._session.flush()
        return entity

    async def soft_delete(self, entity: T) -> None:
        """Mark the entity as deleted and deactivated; the row stays in place.

        Idempotent: deleting an already deleted entity is not an error.

        Raises:
            EntityNotFoundError: ``entity_not_managed``.
        """
        self._ensure_managed(entity)
        entity.is_deleted = True
        entity.is_active = False
        await self._session.flush()

    async def restore(self, entity: T) -> None:
        """Undo a previous soft delete.

        ``is_active`` is intentionally left untouched: it is a business switch
        and restoring a tombstone must not revive a manually disabled entity.

        Raises:
            EntityNotFoundError: ``entity_not_managed``.
        """
        self._ensure_managed(entity)
        entity.is_deleted = False
        await self._session.flush()
