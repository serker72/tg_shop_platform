"""Scoped keyset queries for the Master Bot activity viewer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, func, or_, select, text, true
from sqlalchemy.ext.asyncio import AsyncScalarResult, AsyncSession
from sqlalchemy.orm import aliased

from app.modules.logging.models import UserActivity

Order = Literal["newest", "oldest"]
UI_PAGE_SIZE = 12
EXPORT_BATCH_SIZE = 20


@dataclass(frozen=True, slots=True)
class ActivityScope:
    """Server-generated tenant boundary for every activity query."""

    tenant_ids: frozenset[UUID]
    master_telegram_bot_id: int | None = None
    all_tenants: bool = False
    actor_telegram_id: int | None = None


@dataclass(slots=True)
class ActivityFilter:
    user_id: UUID | None = None
    telegram_user_id: int | None = None
    username: str | None = None
    bot_id: int | None = None
    tenant_id: UUID | None = None
    tenant_ids: frozenset[UUID] | None = None
    allowed_bot_ids: frozenset[int] | None = None
    master_telegram_bot_id: int | None = None
    start_at: datetime = field(default_factory=lambda: datetime.now(UTC) - timedelta(hours=24))
    end_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    action_type: str | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    product_id: UUID | None = None
    category_id: UUID | None = None
    subcategory_id: UUID | None = None
    order_id: UUID | None = None
    case_id: UUID | None = None
    preorder_id: UUID | None = None
    moderator: int | None = None
    visitor: tuple[UUID, int] | None = None


@dataclass(slots=True)
class Page:
    items: list[Any]
    next_cursor: str | None
    previous_cursor: str | None
    has_more: bool
    total: int


@dataclass(frozen=True, slots=True)
class ActivityRow:
    activity: UserActivity
    bot_name: str | None = None


class ActivityQueryRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _applied(
        q: Select[Any] | Select[UserActivity, str | None], f: ActivityFilter, scope: ActivityScope
    ) -> Select[Any] | Select[UserActivity, str | None]:
        if scope is None:
            raise ValueError("activity query requires a server-generated ActivityScope")
        if f.start_at >= f.end_at:
            raise ValueError("start_at must be before end_at")
        q = q.where(UserActivity.created_at >= f.start_at, UserActivity.created_at < f.end_at)
        if f.user_id is not None:
            from app.modules.iam.models import User

            q = q.join(User, User.telegram_user_id == UserActivity.telegram_user_id).where(
                User.id == f.user_id
            )
        if f.telegram_user_id is not None:
            q = q.where(UserActivity.telegram_user_id == f.telegram_user_id)
        if f.visitor is not None:
            tenant, telegram_id = f.visitor
            q = q.where(
                UserActivity.tenant_id == tenant, UserActivity.telegram_user_id == telegram_id
            )
        if f.username is not None:
            # Activity keeps a username snapshot and must remain searchable after the
            # staff IAM row changes or disappears.  The users JOIN is only for the
            # explicit staff/user filter above.
            username = f.username.strip().removeprefix("@").strip().lower()
            if not username:
                q = q.where(UserActivity.username.is_(None))
            else:
                q = q.where(UserActivity.username.icontains(username, autoescape=True))
        if f.bot_id is not None:
            q = q.where(UserActivity.telegram_bot_id == f.bot_id)
        if f.allowed_bot_ids is not None:
            q = q.where(UserActivity.telegram_bot_id.in_(f.allowed_bot_ids))
        if f.tenant_id is not None:
            q = q.where(UserActivity.tenant_id == f.tenant_id)
        if f.tenant_ids is not None:
            q = q.where(UserActivity.tenant_id.in_(f.tenant_ids))
        master = scope.master_telegram_bot_id
        if not scope.all_tenants:
            shops: ColumnElement[bool] = UserActivity.tenant_id.in_(scope.tenant_ids)
            if master is not None:
                shops = and_(shops, UserActivity.telegram_bot_id != master)
                if scope.actor_telegram_id is not None and scope.actor_telegram_id > 0:
                    shops = or_(
                        shops,
                        and_(
                            UserActivity.telegram_bot_id == master,
                            UserActivity.telegram_user_id == scope.actor_telegram_id,
                        ),
                    )
            q = q.where(shops)
        if f.moderator is not None:
            q = q.where(
                UserActivity.telegram_bot_id == master,
                UserActivity.telegram_user_id == f.moderator,
            )
        for name in (
            "action_type",
            "entity_type",
            "product_id",
            "category_id",
            "subcategory_id",
            "order_id",
            "case_id",
            "preorder_id",
            "entity_id",
        ):
            value = getattr(f, name)
            if value is not None:
                q = q.where(getattr(UserActivity, name) == value)
        return q

    def _count_query(
        self, f: ActivityFilter, scope: ActivityScope
    ) -> Select[Any] | Select[UserActivity, str | None]:
        return self._applied(
            select(func.count().label("total")).select_from(UserActivity), f, scope
        )

    async def count(self, filter: ActivityFilter, *, scope: ActivityScope) -> int:
        return int((await self.session.execute(self._count_query(filter, scope))).scalar_one())

    @staticmethod
    def _descending(order: Order, backward: bool = False) -> bool:
        if order not in ("newest", "oldest"):
            raise ValueError("order must be newest or oldest")
        return (order == "newest") != backward

    @asynccontextmanager
    async def stream_export(
        self, f: ActivityFilter, order: Order, *, scope: ActivityScope, limit: int
    ) -> AsyncIterator[AsyncScalarResult[UserActivity]]:
        direction = ColumnElement.desc if self._descending(order) else ColumnElement.asc
        query = (
            self._applied(select(UserActivity), f, scope)
            .order_by(direction(UserActivity.created_at), direction(UserActivity.id))
            .limit(limit)
            .execution_options(yield_per=EXPORT_BATCH_SIZE)
        )
        result = await self.session.stream_scalars(query)
        try:
            yield result
        finally:
            await result.close()

    def _query(
        self,
        f: ActivityFilter,
        order: Order,
        cursor: tuple[datetime, UUID] | None,
        limit: int,
        scope: ActivityScope,
        backward: bool = False,
    ) -> Select[Any] | Select[UserActivity, str | None]:
        from app.modules.bot_manager.models import Bot

        q = self._applied(
            select(UserActivity, Bot.name.label("bot_name")).outerjoin(
                Bot, Bot.telegram_bot_id == UserActivity.telegram_bot_id
            ),
            f,
            scope,
        )
        descending = self._descending(order, backward)
        if cursor:
            ts, ident = cursor
            if not descending:
                q = q.where(
                    or_(
                        UserActivity.created_at > ts,
                        and_(UserActivity.created_at == ts, UserActivity.id > ident),
                    )
                )
            else:
                q = q.where(
                    or_(
                        UserActivity.created_at < ts,
                        and_(UserActivity.created_at == ts, UserActivity.id < ident),
                    )
                )
        if not descending:
            q = q.order_by(UserActivity.created_at.asc(), UserActivity.id.asc())
        else:
            q = q.order_by(UserActivity.created_at.desc(), UserActivity.id.desc())
        return q.limit(limit + 1)

    async def search(
        self,
        filter: ActivityFilter,
        order: Order = "newest",
        cursor: tuple[datetime, UUID] | None = None,
        limit: int = UI_PAGE_SIZE,
        *,
        scope: ActivityScope,
        backward: bool = False,
    ) -> Page:
        if limit not in (UI_PAGE_SIZE, EXPORT_BATCH_SIZE):
            raise ValueError("invalid activity batch size")
        # Both branches share the statement snapshot; the outer join preserves total
        # even when the cursor has no matching rows. Only the page branch is bounded.
        total_rows = self._count_query(filter, scope).subquery()
        page_rows = self._query(filter, order, cursor, limit, scope, backward).subquery()
        activity = aliased(UserActivity, page_rows)
        descending = self._descending(order, backward)
        query = (
            select(activity, page_rows.c.bot_name, total_rows.c.total)
            .select_from(total_rows.outerjoin(page_rows, true()))
            .order_by(
                activity.created_at.desc() if descending else activity.created_at.asc(),
                activity.id.desc() if descending else activity.id.asc(),
            )
        )
        result = (await self.session.execute(query)).all()
        total = int(result[0][2])
        rows = [row for row in result if row[0] is not None]
        more = len(rows) > limit
        rows = rows[:limit]
        if backward:
            rows.reverse()
        items = [
            ActivityRow(
                activity=row[0],
                bot_name=row[1]
                or (
                    "Master Bot"
                    if row[0].telegram_bot_id == scope.master_telegram_bot_id
                    else str(row[0].telegram_bot_id)
                ),
            )
            for row in rows
        ]
        next_cursor = previous_cursor = None
        if items:
            last, first = items[-1].activity, items[0].activity
            if (cursor is not None) if backward else more:
                next_cursor = f"{last.created_at.isoformat()}|{last.id}"
            if more if backward else (cursor is not None):
                previous_cursor = f"{first.created_at.isoformat()}|{first.id}"
        return Page(items, next_cursor, previous_cursor, next_cursor is not None, total)

    async def explain(
        self,
        filter: ActivityFilter,
        order: Order = "newest",
        *,
        scope: ActivityScope,
    ) -> list[Any]:
        """Return PostgreSQL's actual plan for the same bounded query."""
        statement = self._query(filter, order, None, UI_PAGE_SIZE, scope)
        compiled = statement.compile(compile_kwargs={"literal_binds": True})
        result = await self.session.execute(text("EXPLAIN (FORMAT JSON) " + str(compiled)))
        return list(result.scalars())

    @staticmethod
    def decode_cursor(value: str) -> tuple[datetime, UUID]:
        ts, ident = value.split("|", 1)
        return datetime.fromisoformat(ts), UUID(ident)


__all__ = ["ActivityFilter", "ActivityQueryRepository", "ActivityRow", "ActivityScope", "Page"]
