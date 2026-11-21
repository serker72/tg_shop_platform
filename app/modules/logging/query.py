"""Scoped keyset queries for the Master Bot activity viewer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import Select, and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.logging.models import UserActivity

Order = Literal["newest", "oldest"]


@dataclass(frozen=True, slots=True)
class ActivityScope:
    """Server-generated tenant boundary for every activity query."""

    tenant_ids: frozenset[UUID]
    master_telegram_bot_id: int | None = None
    all_tenants: bool = False


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


@dataclass(slots=True)
class Page:
    items: list[Any]
    next_cursor: str | None
    previous_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class ActivityRow:
    activity: UserActivity
    bot_name: str | None = None


class ActivityQueryRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def _query(
        self,
        f: ActivityFilter,
        order: Order,
        cursor: tuple[datetime, UUID] | None,
        limit: int,
        scope: ActivityScope,
        backward: bool = False,
    ) -> Select[Any]:
        from app.modules.bot_manager.models import Bot

        if scope is None:
            raise ValueError("activity query requires a server-generated ActivityScope")
        q = select(UserActivity, Bot.name.label("bot_name")).outerjoin(
            Bot, Bot.telegram_bot_id == UserActivity.telegram_bot_id
        )
        q = q.where(UserActivity.created_at >= f.start_at, UserActivity.created_at < f.end_at)
        if f.user_id is not None:
            from app.modules.iam.models import User

            q = q.join(User, User.telegram_user_id == UserActivity.telegram_user_id).where(
                User.id == f.user_id
            )
        if f.telegram_user_id is not None:
            q = q.where(UserActivity.telegram_user_id == f.telegram_user_id)
        if f.username is not None:
            # Activity keeps a username snapshot and must remain searchable after the
            # staff IAM row changes or disappears.  The users JOIN is only for the
            # explicit staff/user filter above.
            from app.modules.iam.admin_service import normalize_username

            username = normalize_username(f.username)
            if username is None:
                q = q.where(UserActivity.username.is_(None))
            else:
                q = q.where(UserActivity.username.ilike(f"%{username}%"))
        if f.bot_id is not None:
            q = q.where(UserActivity.telegram_bot_id == f.bot_id)
        if f.allowed_bot_ids is not None:
            q = q.where(UserActivity.telegram_bot_id.in_(f.allowed_bot_ids))
        if f.tenant_id is not None:
            q = q.where(UserActivity.tenant_id == f.tenant_id)
        if not scope.all_tenants:
            predicates: list[Any] = [UserActivity.tenant_id.in_(scope.tenant_ids)]
            if scope.master_telegram_bot_id is not None:
                predicates.append(UserActivity.telegram_bot_id == scope.master_telegram_bot_id)
            q = q.where(or_(*predicates))
        if f.moderator is not None:
            q = q.where(
                UserActivity.telegram_bot_id == f.master_telegram_bot_id,
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
        if order not in ("newest", "oldest"):
            raise ValueError("order must be newest or oldest")
        query_order: Order = ("oldest" if order == "newest" else "newest") if backward else order
        if cursor:
            ts, ident = cursor
            if query_order == "oldest":
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
        if query_order == "oldest":
            q = q.order_by(UserActivity.created_at.asc(), UserActivity.id.asc())
        else:
            q = q.order_by(UserActivity.created_at.desc(), UserActivity.id.desc())
        return cast(Select[Any], q.limit(limit + 1))

    async def search(
        self,
        filter: ActivityFilter,
        order: Order = "newest",
        cursor: tuple[datetime, UUID] | None = None,
        limit: int = 20,
        *,
        scope: ActivityScope,
        backward: bool = False,
    ) -> Page:
        if scope is None:
            raise ValueError("activity query requires a server-generated ActivityScope")
        if limit != 20:
            raise ValueError("activity viewer page size is fixed at 20")
        if filter.start_at >= filter.end_at:
            raise ValueError("start_at must be before end_at")
        rows = list(
            (
                await self.session.execute(
                    self._query(filter, order, cursor, limit, scope, backward)
                )
            ).all()
        )
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
                    if row[0].telegram_bot_id == filter.master_telegram_bot_id
                    else str(row[0].telegram_bot_id)
                ),
            )
            for row in rows
        ]
        next_cursor = previous_cursor = None
        if items:
            last, first = items[-1].activity, items[0].activity
            if more:
                next_cursor = f"{last.created_at.isoformat()}|{last.id}"
            if cursor:
                previous_cursor = f"{first.created_at.isoformat()}|{first.id}"
        return Page(items, next_cursor, previous_cursor, more)

    async def explain(
        self,
        filter: ActivityFilter,
        order: Order = "newest",
        *,
        scope: ActivityScope,
    ) -> list[Any]:
        """Return PostgreSQL's actual plan for the same bounded query."""
        statement = self._query(filter, order, None, 20, scope)
        compiled = statement.compile(compile_kwargs={"literal_binds": True})
        result = await self.session.execute(text("EXPLAIN (FORMAT JSON) " + str(compiled)))
        return list(result.scalars())

    @staticmethod
    def decode_cursor(value: str) -> tuple[datetime, UUID]:
        ts, ident = value.split("|", 1)
        return datetime.fromisoformat(ts), UUID(ident)


__all__ = ["ActivityFilter", "ActivityQueryRepository", "ActivityRow", "ActivityScope", "Page"]
