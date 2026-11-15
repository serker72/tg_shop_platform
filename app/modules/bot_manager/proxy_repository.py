"""Data access of the proxy pool. Never commits: the service owns the transaction."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Select, false, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bot_manager.models import PROXY_STATUS_ACTIVE, PROXY_STATUS_QUARANTINE, Bot, Proxy

__all__ = ["ProxyRepository"]


def _locked[S: Select[Any]](stmt: S) -> S:
    """``FOR UPDATE`` + ``populate_existing`` (``expire_on_commit=False`` keeps stale objects)."""
    return stmt.with_for_update().execution_options(populate_existing=True)


def _load() -> ColumnElement[int]:
    """Live bots of the current ``proxies`` row (correlated ``COUNT``)."""
    return (
        select(func.count(Bot.id))
        .where(Bot.proxy_id == Proxy.id, Bot.is_deleted.is_(false()))
        .correlate(Proxy)
        .scalar_subquery()
    )


class ProxyRepository:
    """Data access for :class:`Proxy` and ``bots.proxy_id``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, proxy: Proxy) -> Proxy:
        self._session.add(proxy)
        await self._session.flush()
        return proxy

    async def get(self, proxy_id: UUID, *, for_update: bool = False) -> Proxy | None:
        stmt = select(Proxy).where(Proxy.id == proxy_id, Proxy.is_deleted.is_(false()))
        if for_update:
            stmt = _locked(stmt)
        return (await self._session.scalars(stmt)).one_or_none()

    async def by_url_hash(self, url_hash: str) -> Proxy | None:
        return (
            await self._session.scalars(select(Proxy).where(Proxy.url_hash == url_hash))
        ).one_or_none()

    async def active_ids(self) -> list[UUID]:
        stmt = (
            select(Proxy.id)
            .where(Proxy.status == PROXY_STATUS_ACTIVE, Proxy.is_deleted.is_(false()))
            .order_by(Proxy.id)
        )
        return list(await self._session.scalars(stmt))

    def _available(self, max_bots: int) -> Select[Proxy]:
        load = _load()
        return (
            select(Proxy)
            .where(
                Proxy.status == PROXY_STATUS_ACTIVE,
                Proxy.is_deleted.is_(false()),
                load < max_bots,
            )
            .order_by(Proxy.strike_count, load, Proxy.id)
        )

    async def pick_available(self, max_bots: int) -> Proxy | None:
        """Best ACTIVE proxy with a free slot (no lock): strikes, then load, then id."""
        return (await self._session.scalars(self._available(max_bots).limit(1))).first()

    async def available_ids(self, max_bots: int, *, exclude: UUID | None = None) -> list[UUID]:
        stmt = self._available(max_bots).with_only_columns(Proxy.id)
        if exclude is not None:
            stmt = stmt.where(Proxy.id != exclude)
        return list(await self._session.scalars(stmt))

    async def bot_proxy_id(self, bot_id: UUID) -> tuple[UUID | None] | None:
        """``(proxy_id,)`` of a live bot without a lock; ``None`` - no such bot."""
        stmt = select(Bot.proxy_id).where(Bot.id == bot_id, Bot.is_deleted.is_(false()))
        row = (await self._session.execute(stmt)).first()
        return None if row is None else (row[0],)

    async def bot_count(self, proxy_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(Bot)
            .where(Bot.proxy_id == proxy_id, Bot.is_deleted.is_(false()))
        )
        return int(await self._session.scalar(stmt) or 0)

    async def release_bots(self, proxy_id: UUID) -> tuple[UUID, ...]:
        """``proxy_id = NULL`` for every bot of the proxy; ids of the released bots."""
        stmt = (
            update(Bot)
            .where(Bot.proxy_id == proxy_id)
            .values(proxy_id=None, updated_at=func.now())
            .returning(Bot.id)
            .execution_options(synchronize_session=False)
        )
        return tuple(sorted((await self._session.scalars(stmt)).all()))

    async def rehabilitate(self, banned_before: datetime) -> int:
        """Idempotent: quarantine older than ``banned_before`` -> active, strikes reset."""
        stmt = (
            update(Proxy)
            .where(
                Proxy.status == PROXY_STATUS_QUARANTINE,
                Proxy.last_ban_at < banned_before,
            )
            .values(status=PROXY_STATUS_ACTIVE, strike_count=0, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)
