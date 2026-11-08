"""Data access for the ``system_settings`` singleton row (``id = 1``).

Not a :class:`BaseRepository`: that one is built for ``BusinessEntity`` (UUID key,
soft delete), while :class:`SystemSettings` has the fixed integer key ``1``.

Like ``BaseRepository`` the repository never commits: the caller owns the transaction.
Changes are made through attributes of the managed object returned by :meth:`get` /
:meth:`get_for_update`, followed by ``flush()`` / commit in the caller. ``TimestampMixin``
enables ``eager_defaults``, so ``updated_at`` is fetched by ``RETURNING`` on flush and can be
read afterwards without implicit IO.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.system.models import SINGLETON_ID, SystemSettings
from app.shared_kernel.domain.exceptions import EntityNotFoundError

__all__ = ["SYSTEM_SETTINGS_MISSING", "SystemSettingsRepository"]

#: ``DomainError.code`` raised when the singleton row is absent.
SYSTEM_SETTINGS_MISSING = "system_settings_missing"


class SystemSettingsRepository:
    """Repository of the platform-wide settings singleton."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure_exists(self) -> bool:
        """Insert the singleton row if it is missing; existing values are never touched.

        ``INSERT ... VALUES (1, ...) ON CONFLICT (id) DO NOTHING``: the other columns get
        their model ``default=`` values (identical to the ``server_default`` mirrors),
        ``created_at`` / ``updated_at`` come from the server. Returns ``True`` if the row
        was inserted.

        ``RETURNING id`` instead of ``rowcount``: an ORM-enabled ``insert()`` does not
        report a reliable ``rowcount`` (observed ``0`` for an inserted row).
        """
        stmt = (
            insert(SystemSettings)
            .values(id=SINGLETON_ID)
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(SystemSettings.id)
        )
        inserted = (await self._session.execute(stmt)).scalar_one_or_none()
        return inserted is not None

    async def get(self) -> SystemSettings:
        """Return the singleton row; :class:`EntityNotFoundError` if it is missing.

        No ``populate_existing``: if the row is already in the session's identity map, that
        instance is returned as is and may be stale (changes committed by others or made
        through Core statements are not applied to it). For fresh data use
        :meth:`get_for_update` (refreshes the instance) or a new session.
        """
        return await self._load(for_update=False)

    async def get_for_update(self) -> SystemSettings:
        """Return the singleton row locked with ``SELECT ... FOR UPDATE``."""
        return await self._load(for_update=True)

    async def _load(self, *, for_update: bool) -> SystemSettings:
        stmt = select(SystemSettings).where(SystemSettings.id == SINGLETON_ID)
        if for_update:
            # populate_existing: refresh an already loaded instance with the locked row.
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        entity = (await self._session.execute(stmt)).scalar_one_or_none()
        if entity is None:
            raise EntityNotFoundError(
                f"system_settings has no row with id = {SINGLETON_ID}",
                code=SYSTEM_SETTINGS_MISSING,
            )
        return entity
