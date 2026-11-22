"""Transactional bot management use cases."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from aiogram import Bot as TelegramBot
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bot_manager.commands import BotCommand
from app.modules.bot_manager.models import BOT_STATUS_STARTING, BOT_STATUS_STOPPED, Bot
from app.modules.bot_manager.repository import BotRepository
from app.modules.bot_manager.security import token_hash
from app.modules.bot_manager.sessions import build_bot_session
from app.modules.iam.models import Tenant
from app.shared_kernel.domain.exceptions import (
    DomainError,
    EntityAlreadyExistsError,
    EntityNotFoundError,
    ValidationError,
)

TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
BOT_MANAGE = "bots.manage"


class _Publisher(Protocol):
    async def publish(self, command: BotCommand) -> Any: ...


class _Permissions(Protocol):
    async def require(self, actor: int, slug: str, tenant_id: UUID | None = None) -> Any: ...


class _ProxyPool(Protocol):
    async def get_available_proxy(self) -> Any: ...
    async def assign_proxy_in_transaction(self, bot_id: UUID) -> UUID: ...
    async def release_proxy(self, bot_id: UUID) -> Any: ...


class BotControlService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        permissions: _Permissions | None = None,
        proxy_pool: _ProxyPool | None = None,
        publisher: _Publisher | None = None,
        tenants_use_proxy: bool = False,
    ) -> None:
        self.session, self.permissions, self.proxy_pool, self.publisher = (
            session,
            permissions,
            proxy_pool,
            publisher,
        )
        self.tenants_use_proxy = tenants_use_proxy
        self.repo = BotRepository(session)

    async def _require(self, actor: int, tenant_id: UUID) -> Tenant:
        if self.permissions is not None:
            await self.permissions.require(actor, BOT_MANAGE, tenant_id)
        tenant = (
            await self.session.scalars(select(Tenant).where(Tenant.id == tenant_id))
        ).one_or_none()
        if tenant is None:
            raise EntityNotFoundError("Tenant not found", code="tenant_not_found")
        return tenant

    async def _get_me(self, token: str, proxy_url: str | None = None) -> Any:
        bot = TelegramBot(token, session=build_bot_session(proxy_url))
        try:
            return await bot.get_me()
        except TelegramUnauthorizedError:
            raise DomainError("Invalid bot token", code="invalid_token") from None
        except TelegramAPIError:
            raise DomainError("Telegram is unavailable", code="telegram_unavailable") from None
        finally:
            await bot.session.close()

    async def _publish_transactional(self, command: BotCommand) -> None:
        transactional = getattr(self.publisher, "publish_transactional", None)
        if not getattr(self.publisher, "supports_transactional", False) or transactional is None:
            raise DomainError(
                "Transactional outbox is unavailable", code="transactional_outbox_required"
            )
        await transactional(command, self.session)

    async def _proxy_url(self) -> tuple[str | None, Any | None]:
        if not self.tenants_use_proxy:
            return None, None
        if self.proxy_pool is None:
            raise DomainError("No proxy available", code="no_proxy_available")
        proxy = await self.proxy_pool.get_available_proxy()
        if proxy is None:
            raise DomainError("No proxy available", code="no_proxy_available")
        return proxy.url, proxy

    async def add_bot(self, tenant_id: UUID, token: str, actor: int) -> Bot:
        tenant = await self._require(actor, tenant_id)
        if not tenant.is_active or tenant.is_deleted:
            raise DomainError("Tenant is read-only", code="tenant_inactive")
        token = token.strip()
        if not TOKEN_RE.fullmatch(token):
            raise ValidationError("Invalid bot token format", code="invalid_token_format")
        digest = token_hash(token)
        proxy_url, _ = await self._proxy_url()
        me = await self._get_me(token, proxy_url)
        existing = await self.repo.by_telegram_bot_id(me.id, include_deleted=True, for_update=True)
        if existing is not None and not existing.is_deleted:
            raise EntityAlreadyExistsError("Bot already added", code="bot_already_exists")
        hashed = await self.repo.by_token_hash(digest, for_update=True)
        if hashed is not None and hashed is not existing:
            raise EntityAlreadyExistsError("Bot already added", code="bot_already_exists")
        if existing is None:
            bot = Bot(
                tenant_id=tenant_id,
                telegram_bot_id=me.id,
                username=me.username,
                name=me.first_name,
                encrypted_token=token,
                token_hash=digest,
                status=BOT_STATUS_STOPPED,
            )
            await self.repo.add(bot)
        else:
            bot = existing
            bot.tenant_id, bot.username, bot.name = tenant_id, me.username, me.first_name
            bot.encrypted_token, bot.token_hash = token, digest
            bot.status, bot.is_active, bot.is_deleted, bot.last_error = (
                BOT_STATUS_STOPPED,
                False,
                False,
                None,
            )
            await self.session.flush()
        try:
            if self.tenants_use_proxy and self.proxy_pool is not None:
                await self.proxy_pool.assign_proxy_in_transaction(bot.id)
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise
        return bot

    async def _mutate(self, bot_id: UUID, actor: int, kind: str) -> Bot:
        bot = await self.repo.get(bot_id, for_update=True)
        if bot is None:
            raise EntityNotFoundError("Bot not found", code="bot_not_found")
        tenant = await self._require(actor, bot.tenant_id)
        if not tenant.is_active or tenant.is_deleted:
            raise DomainError("Tenant is read-only", code="tenant_inactive")
        bot.is_active = kind != "STOP_BOT"
        bot.status = BOT_STATUS_STARTING if kind != "STOP_BOT" else BOT_STATUS_STOPPED
        command = BotCommand(
            command_id=uuid4(),
            idempotency_key=uuid4(),
            kind=cast(Literal["START_BOT", "STOP_BOT", "RESTART_BOT"], kind),
            bot_id=bot.id,
            issued_at=datetime.now(UTC),
            issued_by=actor,
        )
        await self._publish_transactional(command)
        await self.session.commit()
        return bot

    async def start(self, bot_id: UUID, actor: int) -> Bot:
        return await self._mutate(bot_id, actor, "START_BOT")

    async def stop(self, bot_id: UUID, actor: int) -> Bot:
        return await self._mutate(bot_id, actor, "STOP_BOT")

    async def restart(self, bot_id: UUID, actor: int) -> Bot:
        return await self._mutate(bot_id, actor, "RESTART_BOT")

    async def replace_token(self, bot_id: UUID, new_token: str, actor: int) -> Bot:
        bot = await self.repo.get(bot_id, for_update=True)
        if bot is None:
            raise EntityNotFoundError("Bot not found", code="bot_not_found")
        tenant = await self._require(actor, bot.tenant_id)
        if not tenant.is_active or tenant.is_deleted:
            raise DomainError("Tenant is read-only", code="tenant_inactive")
        if not TOKEN_RE.fullmatch(new_token.strip()):
            raise ValidationError("Invalid bot token format", code="invalid_token_format")
        proxy_url, _ = await self._proxy_url()
        me = await self._get_me(new_token.strip(), proxy_url)
        if me.id != bot.telegram_bot_id:
            raise DomainError("Bot identity mismatch", code="bot_identity_mismatch")
        bot.encrypted_token, bot.token_hash, bot.username, bot.name = (
            new_token.strip(),
            token_hash(new_token),
            me.username,
            me.first_name,
        )
        command = BotCommand(
            command_id=uuid4(),
            idempotency_key=uuid4(),
            kind="RESTART_BOT",
            bot_id=bot.id,
            issued_at=datetime.now(UTC),
            issued_by=actor,
        )
        await self._publish_transactional(command)
        await self.session.commit()
        return bot

    async def deactivate(self, bot_id: UUID, actor: int) -> Bot:
        bot = await self.repo.get(bot_id, for_update=True)
        if bot is None:
            raise EntityNotFoundError("Bot not found", code="bot_not_found")
        await self._require(actor, bot.tenant_id)
        bot.is_active, bot.is_deleted = False, True
        await self.session.commit()
        if self.proxy_pool is not None:
            await self.proxy_pool.release_proxy(bot.id)
        return bot

    async def list_for_tenant(self, actor: int, tenant_id: UUID) -> list[Bot]:
        await self._require(actor, tenant_id)
        return await self.repo.list_for_tenants([tenant_id])


__all__ = ["BotControlService", "TOKEN_RE"]
