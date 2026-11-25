"""Tenant bot mutations and their transactional command outbox."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Protocol
from uuid import UUID, uuid4

from aiogram import Bot as TelegramBot
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import User
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bot_manager.commands import BotCommand, BotCommandKind
from app.modules.bot_manager.models import (
    BOT_STATUS_RUNNING,
    BOT_STATUS_STARTING,
    BOT_STATUS_STOPPED,
    Bot,
    Proxy,
)
from app.modules.bot_manager.proxy_repository import ProxyRepository
from app.modules.bot_manager.proxy_service import (
    NoProxyAvailableError,
    ProxyPoolService,
    ProxyUrlChangedError,
    ValidatedProxyAssignment,
)
from app.modules.bot_manager.publisher import BotCommandPublisher
from app.modules.bot_manager.repository import BotRepository
from app.modules.bot_manager.security import token_hash
from app.modules.bot_manager.sessions import build_bot_session, mask_proxy_url
from app.modules.iam.models import Tenant
from app.modules.logging.audit import SqlAuditLogger
from app.modules.logging.events import AuditEvent
from app.shared_kernel.domain.exceptions import (
    DomainError,
    EntityAlreadyExistsError,
    EntityNotFoundError,
    ValidationError,
)

TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$", re.ASCII)
BOT_MANAGE = "bots.manage"


class _Permissions(Protocol):
    async def require(self, actor: int, slug: str, tenant_id: UUID | None = None) -> object: ...


@dataclass(frozen=True, slots=True)
class BotPage:
    items: list[Bot]
    index: int
    page_count: int
    read_only: bool


class BotControlService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        permissions: _Permissions | None = None,
        proxy_pool: ProxyPoolService | None = None,
        publisher: BotCommandPublisher | None = None,
        tenants_use_proxy: bool = False,
    ) -> None:
        self.session = session
        self.permissions = permissions
        self.proxy_pool = proxy_pool
        self.publisher = publisher
        self.tenants_use_proxy = tenants_use_proxy
        self.repo = BotRepository(session)
        self.audit = SqlAuditLogger()

    async def _require(self, actor: int, tenant_id: UUID, *, write: bool = False) -> Tenant:
        if self.permissions is None:
            raise DomainError("Permissions unavailable", code="permission_denied")
        await self.permissions.require(actor, BOT_MANAGE, tenant_id)
        stmt = select(Tenant).where(Tenant.id == tenant_id)
        if write:
            # Serialize against tenant deactivation/deletion; reread any cached ORM row.
            stmt = stmt.with_for_update(read=True).execution_options(populate_existing=True)
        tenant = (await self.session.scalars(stmt)).one_or_none()
        if tenant is None:
            raise EntityNotFoundError("Tenant not found", code="tenant_not_found")
        if write and (not tenant.is_active or tenant.is_deleted):
            raise DomainError("Tenant is read-only", code="tenant_inactive")
        return tenant

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        try:
            yield
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            # Do not chain SQLAlchemy errors: statement parameters are not user-facing.
            if getattr(exc.orig, "sqlstate", None) == "23505":
                raise EntityAlreadyExistsError(
                    "Bot already added", code="bot_already_exists"
                ) from None
            raise DomainError("Bot persistence failed", code="bot_save_failed") from None
        except SQLAlchemyError:
            await self.session.rollback()
            raise DomainError("Bot persistence failed", code="bot_save_failed") from None
        except BaseException:
            await self.session.rollback()
            raise

    async def _get_me(self, token: str, proxy_url: str | None) -> User:
        try:
            async with build_bot_session(proxy_url) as session:
                bot = TelegramBot(token, session=session)
                return await bot.get_me(request_timeout=10)
        except TelegramUnauthorizedError:
            raise DomainError("Invalid bot token", code="invalid_token") from None
        except Exception:
            # Telegram/network exceptions can carry request URLs and proxy credentials.
            raise DomainError("Telegram is unavailable", code="telegram_unavailable") from None

    async def _command(self, kind: BotCommandKind, bot: Bot, actor: int) -> None:
        if self.publisher is None or not self.publisher.supports_transactional:
            raise DomainError(
                "Transactional outbox is unavailable", code="transactional_outbox_required"
            )
        key = uuid4()
        await self.publisher.publish_transactional(
            BotCommand(
                command_id=key,
                idempotency_key=key,
                kind=kind,
                bot_id=bot.id,
                issued_at=datetime.now(UTC),
                issued_by=actor,
            ),
            self.session,
        )

    async def _audit(self, action: str, bot: Bot, actor: int) -> None:
        await self.audit.record(
            self.session,
            AuditEvent(
                actor_type="user",
                actor_id=actor,
                action=action,
                entity_type="bot",
                entity_id=bot.id,
                tenant_id=bot.tenant_id,
                telegram_bot_id=bot.telegram_bot_id,
            ),
        )

    @staticmethod
    def _token(token: str) -> str:
        token = token.strip()
        if not TOKEN_RE.fullmatch(token):
            raise ValidationError("Invalid bot token format", code="invalid_token_format")
        return token

    async def _proxy(
        self, current_id: UUID | None = None, *, exclude: set[UUID] | None = None
    ) -> Proxy | None:
        if not self.tenants_use_proxy:
            return None
        proxy = (
            await self.proxy_pool.get_available_proxy(current_id, exclude=exclude)
            if self.proxy_pool
            else None
        )
        if proxy is None:
            raise DomainError("No proxy available", code="no_proxy_available")
        return proxy

    async def _add_with_proxy_retry(
        self, token: str, save_bot: Callable[[User], Awaitable[Bot]]
    ) -> Bot:
        """Repeat getMe after a concurrent slot loss or URL change."""
        if self.proxy_pool is None:
            raise NoProxyAvailableError
        attempted: set[UUID] = set()
        for _ in range(3):
            proxy = await self.proxy_pool.get_available_proxy(exclude=attempted)
            if proxy is None:
                break
            proxy_id, url = proxy.id, proxy.url
            attempted.add(proxy_id)
            me = await self._get_me(token, url)
            try:
                return await self.proxy_pool.assign_proxy(
                    ValidatedProxyAssignment(proxy_id, url, partial(save_bot, me))
                )
            except (NoProxyAvailableError, ProxyUrlChangedError) as exc:
                if isinstance(exc, ProxyUrlChangedError):
                    # The savepoint has rolled back; the same proxy can be picked again.
                    attempted.remove(proxy_id)
                # A concurrent add with a different token can still have the same Telegram ID.
                # Only live rows block addition.
                if await self.repo.by_telegram_bot_id(me.id) is not None:
                    raise EntityAlreadyExistsError(
                        "Bot already added", code="bot_already_exists"
                    ) from None
        raise NoProxyAvailableError

    async def _save_unbound_bot(
        self, tenant_id: UUID, token: str, digest: str, me: User
    ) -> tuple[Bot, bool]:
        existing = await self.repo.by_telegram_bot_id(me.id, include_deleted=True, for_update=True)
        duplicate = await self.repo.by_token_hash(digest)
        if (existing is not None and not existing.is_deleted) or (
            duplicate is not None and duplicate is not existing
        ):
            raise EntityAlreadyExistsError("Bot already added", code="bot_already_exists")
        bot = existing if existing is not None else Bot(telegram_bot_id=me.id)
        bot.tenant_id, bot.username, bot.name = tenant_id, me.username, me.first_name
        bot.encrypted_token, bot.token_hash = token, digest
        bot.proxy_id = None
        bot.is_active, bot.is_deleted = False, False
        if existing is None:
            bot.status = BOT_STATUS_STOPPED
            bot.status_changed_at = datetime.now(UTC)
            await self.repo.add(bot)
        else:
            await self.repo.set_status(bot, BOT_STATUS_STOPPED)
        return bot, existing is not None

    async def add_bot(self, tenant_id: UUID, token: str, actor: int) -> Bot:
        async with self._transaction():
            await self._require(actor, tenant_id, write=True)
            token = self._token(token)
            digest = token_hash(token)
            known = await self.repo.by_token_hash(digest)
            if known is not None and not known.is_deleted:
                raise EntityAlreadyExistsError("Bot already added", code="bot_already_exists")
            restored = False

            async def save_bot(me: User) -> Bot:
                nonlocal restored
                bot, restored = await self._save_unbound_bot(tenant_id, token, digest, me)
                return bot

            if self.tenants_use_proxy:
                try:
                    bot = await self._add_with_proxy_retry(token, save_bot)
                except NoProxyAvailableError:
                    latest = await self.repo.by_token_hash(digest, refresh=True)
                    if latest is not None and not latest.is_deleted:
                        raise EntityAlreadyExistsError(
                            "Bot already added", code="bot_already_exists"
                        ) from None
                    raise
            else:
                bot = await save_bot(await self._get_me(token, None))
            await self._audit("bot.restored" if restored else "bot.added", bot, actor)
        return bot

    async def _locked_bot(self, bot_id: UUID, actor: int, tenant_id: UUID | None) -> Bot:
        if tenant_id is None:
            candidate = await self.repo.get(bot_id)
            if candidate is None:
                raise EntityNotFoundError("Bot not found", code="bot_not_found")
            tenant_id = candidate.tenant_id
        await self._require(actor, tenant_id, write=True)
        bot = await self.repo.get_for_tenant(tenant_id, bot_id, for_update=True)
        if bot is None:
            raise EntityNotFoundError("Bot not found", code="bot_not_found")
        return bot

    async def _mutate(
        self, bot_id: UUID, actor: int, kind: BotCommandKind, tenant_id: UUID | None
    ) -> Bot:
        async with self._transaction():
            bot = await self._locked_bot(bot_id, actor, tenant_id)
            if (
                kind == "START_BOT"
                and bot.is_active
                and bot.status in (BOT_STATUS_STARTING, BOT_STATUS_RUNNING)
            ):
                return bot
            bot.is_active = kind != "STOP_BOT"
            await self.repo.set_status(
                bot, BOT_STATUS_STARTING if bot.is_active else BOT_STATUS_STOPPED
            )
            await self._command(kind, bot, actor)
            await self._audit(f"bot.{kind.removesuffix('_BOT').lower()}", bot, actor)
        return bot

    async def start(self, bot_id: UUID, actor: int, *, tenant_id: UUID | None = None) -> Bot:
        return await self._mutate(bot_id, actor, "START_BOT", tenant_id)

    async def stop(self, bot_id: UUID, actor: int, *, tenant_id: UUID | None = None) -> Bot:
        return await self._mutate(bot_id, actor, "STOP_BOT", tenant_id)

    async def restart(self, bot_id: UUID, actor: int, *, tenant_id: UUID | None = None) -> Bot:
        return await self._mutate(bot_id, actor, "RESTART_BOT", tenant_id)

    async def replace_token(
        self, bot_id: UUID, new_token: str, actor: int, *, tenant_id: UUID | None = None
    ) -> Bot:
        async with self._transaction():
            bot = await self._locked_bot(bot_id, actor, tenant_id)
            token = self._token(new_token)
            proxy = await self._proxy(bot.proxy_id)
            me = await self._get_me(token, proxy.url if proxy else None)
            if me.id != bot.telegram_bot_id:
                raise DomainError("Bot identity mismatch", code="bot_identity_mismatch")
            bot.encrypted_token, bot.token_hash = token, token_hash(token)
            bot.username, bot.name = me.username, me.first_name
            await self.repo.set_status(
                bot, BOT_STATUS_STARTING if bot.is_active else BOT_STATUS_STOPPED
            )
            await self._command("RESTART_BOT", bot, actor)
            await self._audit("bot.token_replaced", bot, actor)
        return bot

    async def deactivate(self, bot_id: UUID, actor: int, *, tenant_id: UUID | None = None) -> Bot:
        async with self._transaction():
            bot = await self._locked_bot(bot_id, actor, tenant_id)
            bot.is_active, bot.is_deleted, bot.proxy_id = False, True, None
            await self.repo.set_status(bot, BOT_STATUS_STOPPED)
            await self._command("STOP_BOT", bot, actor)
            await self._audit("bot.deleted", bot, actor)
        return bot

    async def list_for_tenant(self, actor: int, tenant_id: UUID) -> list[Bot]:
        await self._require(actor, tenant_id)
        return await self.repo.list_for_tenants([tenant_id])

    async def page_for_tenant(self, actor: int, tenant_id: UUID, *, page: int) -> BotPage:
        tenant = await self._require(actor, tenant_id)
        size = 8
        read_only = not tenant.is_active or tenant.is_deleted
        rows, index, pages = await self.repo.snapshot_page_for_tenant(tenant_id, size, page)
        return BotPage(rows, index, pages, read_only)

    async def get_for_tenant(self, actor: int, tenant_id: UUID, bot_id: UUID) -> Bot:
        await self._require(actor, tenant_id)
        bot = await self.repo.get_for_tenant(tenant_id, bot_id)
        if bot is None:
            raise EntityNotFoundError("Bot not found", code="bot_not_found")
        return bot

    async def proxy_display(self, bot: Bot) -> str | None:
        if bot.proxy_id is None:
            return None
        proxy = await ProxyRepository(self.session).get(bot.proxy_id)
        return mask_proxy_url(proxy.url) if proxy is not None else None

    async def tenant_is_read_only(self, actor: int, tenant_id: UUID) -> bool:
        tenant = await self._require(actor, tenant_id)
        return not tenant.is_active or tenant.is_deleted


__all__ = ["BotControlService", "TOKEN_RE"]
