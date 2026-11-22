"""Read-only filter choices for the activity viewer (not an entity admin API)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Select, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bot_manager.models import Bot, Customer
from app.modules.iam.models import Tenant, TenantModerator, User
from app.modules.logging.query import ActivityScope

OptionKind = Literal["shop", "bot", "staff", "visitor", "moderator"]
OPTION_PAGE_SIZE = 8


@dataclass(frozen=True, slots=True)
class FilterOption:
    key: str
    label: str
    tenant_id: UUID | None


class ActivityFilterOptions:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _query(
        kind: OptionKind, scope: ActivityScope, tenant: UUID | None
    ) -> tuple[Select[Any], Any]:
        stmt: Any
        key: Any
        tenant_column: Any
        if kind == "shop":
            key = Tenant.id
            stmt = select(key, Tenant.name, Tenant.id).where(Tenant.is_deleted.is_(False))
            tenant_column = Tenant.id
        elif kind == "bot":
            key = Bot.telegram_bot_id
            stmt = select(key, Bot.name, Bot.tenant_id).join(Tenant, Tenant.id == Bot.tenant_id)
            stmt = stmt.where(Bot.is_deleted.is_(False), Tenant.is_deleted.is_(False))
            tenant_column = Bot.tenant_id
        elif kind == "visitor":
            key = Customer.telegram_user_id
            stmt = select(key, Customer.username, Customer.tenant_id).join(
                Tenant, Tenant.id == Customer.tenant_id
            )
            stmt = stmt.where(Tenant.is_deleted.is_(False), Customer.tenant_id == tenant)
            tenant_column = Customer.tenant_id
        else:
            key = User.id if kind == "staff" else User.telegram_user_id
            stmt = select(key, User.telegram_user_name, User.telegram_user_id).where(
                User.is_deleted.is_(False)
            )
            if kind == "moderator" and scope.master_telegram_bot_id is None:
                stmt = stmt.where(false())
            if not scope.all_tenants:
                own = User.telegram_user_id == scope.actor_telegram_id
                shared = select(TenantModerator.user_id).where(
                    TenantModerator.tenant_id.in_(scope.tenant_ids)
                )
                stmt = stmt.where(own if kind == "moderator" else or_(own, User.id.in_(shared)))
            return stmt, key
        if not scope.all_tenants:
            stmt = stmt.where(tenant_column.in_(scope.tenant_ids))
        if tenant is not None:
            stmt = stmt.where(tenant_column == tenant)
        return stmt, key

    @staticmethod
    def _option(kind: OptionKind, row: Any) -> FilterOption:
        key, label, owner = row
        if kind in ("staff", "moderator", "visitor") and label:
            label = "@" + label.lstrip("@")
        return FilterOption(
            str(key),
            label or str(owner if kind == "staff" else key),
            owner if isinstance(owner, UUID) else None,
        )

    async def page(
        self, kind: OptionKind, scope: ActivityScope, tenant: UUID | None, offset: int
    ) -> tuple[list[FilterOption], bool]:
        stmt, key = self._query(kind, scope, tenant)
        rows = (
            await self.session.execute(
                stmt.order_by(key).offset(offset).limit(OPTION_PAGE_SIZE + 1)
            )
        ).all()
        return [self._option(kind, row) for row in rows[:OPTION_PAGE_SIZE]], len(
            rows
        ) > OPTION_PAGE_SIZE

    async def get(
        self, kind: OptionKind, scope: ActivityScope, tenant: UUID | None, value: str
    ) -> FilterOption | None:
        try:
            ident: UUID | int = UUID(value) if kind in ("shop", "staff") else int(value)
            if isinstance(ident, int) and not 0 < ident < 2**63:
                return None
        except (ValueError, TypeError):
            return None
        stmt, key = self._query(kind, scope, tenant)
        row = (await self.session.execute(stmt.where(key == ident))).one_or_none()
        return self._option(kind, row) if row else None
