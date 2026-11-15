"""Repositories of the bot manager. They never commit: the caller owns the transaction."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, false, func, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bot_manager.models import (
    BOT_STATUS_INVALID_TOKEN,
    BOT_STATUSES,
    Bot,
    TenantCustomer,
)

__all__ = ["BotRepository", "TenantCustomerRepository"]


def _locked[S: Select[Any]](stmt: S) -> S:
    """``FOR UPDATE`` + ``populate_existing``: the locked row overwrites a stale object of
    the identity map (``expire_on_commit=False`` keeps objects of earlier transactions)."""
    return stmt.with_for_update().execution_options(populate_existing=True)


class BotRepository:
    """Data access for :class:`Bot`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _one(self, stmt: Select[Bot], *, for_update: bool) -> Bot | None:
        if for_update:
            stmt = _locked(stmt)
        return (await self._session.scalars(stmt)).one_or_none()

    async def add(self, bot: Bot) -> Bot:
        """Add and flush (unique/CHECK violations surface here)."""
        self._session.add(bot)
        await self._session.flush()
        return bot

    async def get(
        self, bot_id: UUID, *, include_deleted: bool = False, for_update: bool = False
    ) -> Bot | None:
        stmt = select(Bot).where(Bot.id == bot_id)
        if not include_deleted:
            stmt = stmt.where(Bot.is_deleted.is_(false()))
        return await self._one(stmt, for_update=for_update)

    async def by_token_hash(self, token_hash: str, *, for_update: bool = False) -> Bot | None:
        """Lookup by ``token_hash`` (unique across all rows, soft-deleted included)."""
        return await self._one(
            select(Bot).where(Bot.token_hash == token_hash), for_update=for_update
        )

    async def by_telegram_bot_id(
        self, telegram_bot_id: int, *, include_deleted: bool = False, for_update: bool = False
    ) -> Bot | None:
        """Lookup by Telegram ID; ``include_deleted=True`` finds a bot to restore."""
        stmt = select(Bot).where(Bot.telegram_bot_id == telegram_bot_id)
        if not include_deleted:
            stmt = stmt.where(Bot.is_deleted.is_(false()))
        return await self._one(stmt, for_update=for_update)

    async def list_for_tenants(self, tenant_ids: Collection[UUID] | None) -> list[Bot]:
        """Live bots of the given tenants; ``None`` means all tenants (global access)."""
        stmt = select(Bot).where(Bot.is_deleted.is_(false()))
        if tenant_ids is not None:
            if not tenant_ids:
                return []
            stmt = stmt.where(Bot.tenant_id.in_(list(tenant_ids)))
        stmt = stmt.order_by(Bot.created_at, Bot.id)
        return list(await self._session.scalars(stmt))

    async def list_runnable(self) -> list[Bot]:
        """Live, enabled bots whose token is not known to be invalid."""
        stmt = (
            select(Bot)
            .where(
                Bot.is_deleted.is_(false()),
                Bot.is_active.is_(true()),
                Bot.status != BOT_STATUS_INVALID_TOKEN,
            )
            .order_by(Bot.created_at, Bot.id)
        )
        return list(await self._session.scalars(stmt))

    async def set_status(self, bot: Bot, status: str, *, error: str | None = None) -> Bot:
        """Set ``status`` / ``last_error`` / ``status_changed_at`` and flush."""
        if status not in BOT_STATUSES:
            raise ValueError(f"unknown bot status: {status!r}")
        bot.status = status
        bot.last_error = error
        bot.status_changed_at = datetime.now(UTC)
        await self._session.flush()
        return bot


class TenantCustomerRepository:
    """Data access for :class:`TenantCustomer`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        tenant_id: UUID,
        telegram_user_id: int,
        username: str | None,
        language_code: str | None = None,
        *,
        first_bot_id: UUID | None = None,
    ) -> TenantCustomer:
        """Insert or refresh the customer row of ``(tenant_id, telegram_user_id)``.

        ``username`` is overwritten, ``language_code`` only when a new value is given,
        ``first_bot_id`` is set once and never changed afterwards.
        """
        stmt = pg_insert(TenantCustomer).values(
            tenant_id=tenant_id,
            telegram_user_id=telegram_user_id,
            username=username,
            language_code=language_code,
            first_bot_id=first_bot_id,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[TenantCustomer.tenant_id, TenantCustomer.telegram_user_id],
            set_={
                "username": stmt.excluded.username,
                "language_code": func.coalesce(
                    stmt.excluded.language_code, TenantCustomer.language_code
                ),
                "first_bot_id": func.coalesce(
                    TenantCustomer.first_bot_id, stmt.excluded.first_bot_id
                ),
                "updated_at": func.now(),
            },
        )
        result = await self._session.scalars(
            stmt.returning(TenantCustomer), execution_options={"populate_existing": True}
        )
        return result.one()
