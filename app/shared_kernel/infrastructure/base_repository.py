"""Generic async repository over :class:`sqlalchemy.ext.asyncio.AsyncSession`.

Design rules:

* the repository never calls ``commit()`` - transaction boundaries belong to
  the unit-of-work / request handler, so several repositories can participate
  in one atomic operation;
* physical ``DELETE`` is forbidden: business entities are only soft-deleted,
  therefore no ``delete()`` method is provided on purpose;
* every read method filters out tombstones (``is_deleted IS false``) unless the
  caller explicitly opts in with ``include_deleted=True``;

Tenant isolation is explicit (TZ, lines 111-124): the repository has no ``tenant_id``
filter of its own. A tenant-scoped ``list()`` passes :class:`TenantScope` together with a
``tenant_of`` accessor and fails with :class:`TenantIsolationError` on a foreign row. A
full tenant-aware repository (scope in the constructor, filter in every query) is planned
for milestone III - together with the tenant catalogue, the first entity that needs it.
"""

from __future__ import annotations

import uuid
import uuid as uuid_lib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, Select, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.base import NO_VALUE

from app.shared_kernel.domain.base_entity import BusinessEntity
from app.shared_kernel.domain.exceptions import EntityNotFoundError, TenantIsolationError
from app.shared_kernel.domain.tenant import TenantScope

__all__ = ["MAX_SCOPED_LIMIT", "BaseRepository", "ScopedPage"]

#: Hard cap of a tenant-scoped page: a scoped read is never "the whole table".
#: Without a tenant filter a big page is not just slow - it is a cross-tenant leak surface.
MAX_SCOPED_LIMIT = 100


@dataclass(frozen=True, slots=True)
class ScopedPage[T: BusinessEntity]:
    """Rows of a tenant-scoped read plus the truncation flag (decision RC-3-A).

    A scoped read never silently drops rows. The tenant filter is applied **in SQL**
    (``tenant_column``), so the page window only contains rows of the requested tenant and
    foreign rows can neither hide own ones nor distort ``truncated``. One extra row is
    fetched to report ``truncated=True`` when more exist.

    Attributes:
        rows: rows of the requested tenant (each one verified with ``ensure_owns``).
        truncated: ``True`` when at least one more row of this tenant matched.
        limit: effective page size (``min(limit, MAX_SCOPED_LIMIT)``).
    """

    rows: Sequence[T]
    truncated: bool
    limit: int


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
        scope: TenantScope | None = None,
        tenant_of: Callable[[T], uuid_lib.UUID | None] | None = None,
        tenant_column: str | None = None,
    ) -> Sequence[T] | ScopedPage[T]:
        """Return a page of entities ordered by ``created_at`` then ``id``.

        ``id`` is a tie-breaker: rows created in one transaction share ``now()``,
        and without it ``LIMIT/OFFSET`` pagination is not deterministic.

        Two shapes:

        * **platform-wide** (``scope`` and ``tenant_of`` both omitted) - a plain
          ``Sequence[T]``, used by admin views and counterparty catalogues;
        * **tenant-scoped** (``scope`` given) - a :class:`ScopedPage`, i.e. the rows of the
          caller's tenant plus ``truncated``. The tenant filter goes into SQL through
          ``tenant_column``, so *foreign rows never enter the page window*: they cannot
          hide the caller's rows nor distort ``truncated``. Every returned row is verified
          with :meth:`TenantScope.ensure_owns` as a second line of defence, and the page is
          capped by :data:`MAX_SCOPED_LIMIT`. The cap is never silent: ``truncated=True``
          tells the caller that more rows of this tenant matched (decision RC-3-A).

        A scoped read **requires** ``tenant_column``. ``tenant_of`` alone cannot filter in
        SQL (the tenant id may be derived from a relationship and no column exists), and a
        page window over an unfiltered query is incorrect: foreign rows would be loaded and
        ``truncated`` would lie. Passing only ``tenant_of`` is therefore refused
        (``tenant_scope_required``) instead of silently degrading into "all tenants".

        Args:
            include_deleted: also return soft-deleted rows when ``True``.
            limit: desired page size; ``None`` means no limit for an unscoped read.
            offset: number of rows to skip.
            scope: tenant context of the caller.
            tenant_of: how to read ``tenant_id`` of a row (``lambda row: row.tenant_id``),
                used for the post-read verification.
            tenant_column: SQL column holding the tenant id (``"tenant_id"``); pushed into
                the ``WHERE`` clause of a scoped read.

        Raises:
            TenantIsolationError: ``tenant_scope_required`` - only one of ``scope`` /
                ``tenant_of`` was given; ``tenant_mismatch`` - a row of a foreign tenant.

        Returns:
            ``Sequence[T]`` for a platform-wide read, :class:`ScopedPage` when scoped.
        """
        if (scope is None) != (tenant_of is None):
            raise TenantIsolationError(
                "tenant-scoped read needs both scope and tenant_of",
                code="tenant_scope_required",
            )
        if scope is None and tenant_column is not None:
            # A tenant column without a scope has no value to filter by: refusing is safer
            # than silently returning a platform-wide page the caller thinks is scoped.
            raise TenantIsolationError(
                "tenant_column needs scope and tenant_of",
                code="tenant_scope_required",
            )
        if scope is None:
            return await self._page(include_deleted=include_deleted, limit=limit, offset=offset)
        if tenant_of is None:  # narrowed by the check above; keeps mypy informed
            raise TenantIsolationError(
                "tenant-scoped read needs both scope and tenant_of",
                code="tenant_scope_required",
            )
        tenant_filter = self._tenant_filter(tenant_column)
        effective = MAX_SCOPED_LIMIT if limit is None else min(limit, MAX_SCOPED_LIMIT)
        # ``limit + 1`` detects "there is more" without counting the whole table. The
        # filter is applied *before* the window, so foreign rows cannot consume it.
        fetched = await self._page(
            include_deleted=include_deleted,
            limit=effective + 1,
            offset=offset,
            tenant_filter=tenant_filter(scope.tenant_id),
        )
        page = fetched[:effective]
        for entity in page:
            entity_tenant_id = tenant_of(entity)
            if entity_tenant_id is None:
                raise TenantIsolationError(
                    f"{type(entity).__name__} has no tenant_id", code="tenant_mismatch"
                )
            scope.ensure_owns(entity_tenant_id)
        return ScopedPage(rows=page, truncated=len(fetched) > effective, limit=effective)

    def _tenant_filter(
        self, tenant_column: str | None
    ) -> Callable[[uuid_lib.UUID], ColumnElement[bool]]:
        """Return a ``WHERE`` builder for ``tenant_column`` of the model.

        Raises:
            TenantIsolationError: ``tenant_scope_required`` when ``tenant_column`` is
                missing - a scoped page must be filtered in SQL, see :meth:`list`;
                ``tenant_column_unknown`` when the model has no such column.
        """
        if not tenant_column:
            raise TenantIsolationError(
                "tenant-scoped read needs tenant_column to filter in SQL "
                "(tenant_of alone cannot: foreign rows would hide own ones)",
                code="tenant_scope_required",
            )
        # Only a mapped *column* qualifies: ``getattr`` would also accept relationships,
        # properties or methods and build a bogus ``WHERE``.
        mapped = inspect(self._model).columns.get(tenant_column)
        column = getattr(self._model, tenant_column) if mapped is not None else None
        if column is None:
            raise TenantIsolationError(
                f"{self._model.__name__} has no column {tenant_column!r}",
                code="tenant_column_unknown",
            )
        return lambda tenant_id: column == tenant_id

    async def _page(
        self,
        *,
        include_deleted: bool,
        limit: int | None,
        offset: int,
        tenant_filter: ColumnElement[bool] | None = None,
    ) -> Sequence[T]:
        """Single ``SELECT`` of a page, no tenant verification."""
        stmt = select(self._model) if include_deleted else self._base_stmt()
        if tenant_filter is not None:
            stmt = stmt.where(tenant_filter)
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
