"""Unit tests of the bot manager models (no database)."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import fields
from typing import cast

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Table, UniqueConstraint

from app.modules.bot_manager.models import BOT_STATUSES, Bot, TenantCustomer
from app.modules.bot_manager.security import token_hash
from app.modules.logging.events import ActivityEvent, AuditEvent
from app.modules.logging.files import event_paths

pytestmark = pytest.mark.req("R-II.7")

TOKEN = "123456:AA-secret-token-value"  # noqa: S105


def test_repr_has_no_token() -> None:
    bot = Bot(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        telegram_bot_id=42,
        encrypted_token=TOKEN,
        token_hash=token_hash(TOKEN),
        status="stopped",
    )
    text = repr(bot)
    assert TOKEN not in text
    assert token_hash(TOKEN) not in text
    assert "telegram_bot_id=42" in text


def test_token_hash_is_sha256_hex() -> None:
    assert token_hash(TOKEN) == hashlib.sha256(TOKEN.encode()).hexdigest()
    assert len(token_hash(TOKEN)) == 64
    assert token_hash(f"  {TOKEN}\n") == token_hash(TOKEN)


def test_bot_table_contract() -> None:
    table = cast(Table, Bot.__table__)
    assert not table.c.tenant_id.nullable
    assert not table.c.telegram_bot_id.nullable
    assert not table.c.encrypted_token.nullable
    assert table.c.proxy_id.nullable
    assert "kind" not in table.c
    uniques = {
        tuple(c.name for c in u.columns)
        for u in table.constraints
        if isinstance(u, UniqueConstraint)
    }
    assert {("telegram_bot_id",), ("token_hash",)} <= uniques
    checks = [c for c in table.constraints if isinstance(c, CheckConstraint)]
    assert [c.name for c in checks] == ["ck_bots_status"]
    assert all(f"'{status}'" in str(checks[0].sqltext) for status in BOT_STATUSES)
    ondelete = {
        next(iter(c.columns)).name: c.ondelete
        for c in table.constraints
        if isinstance(c, ForeignKeyConstraint)
    }
    assert ondelete == {"tenant_id": "RESTRICT", "proxy_id": "SET NULL"}
    assert {i.name for i in table.indexes} >= {
        "ix_bots_tenant_id",
        "ix_bots_status",
        "ix_bots_proxy_id",
    }


def test_tenant_customer_contract() -> None:
    table = cast(Table, TenantCustomer.__table__)
    ondelete = {
        next(iter(c.columns)).name: c.ondelete
        for c in table.constraints
        if isinstance(c, ForeignKeyConstraint)
    }
    assert ondelete == {
        "tenant_id": "CASCADE",
        "language_code": "RESTRICT",
        "first_bot_id": "SET NULL",
    }
    assert table.c.language_code.nullable
    assert "uq_tenant_customers_tenant_id_telegram_user_id" in {
        c.name for c in table.constraints if isinstance(c, UniqueConstraint)
    }


def test_log_events_carry_telegram_bot_id() -> None:
    assert "telegram_bot_id" in {f.name for f in fields(ActivityEvent)}
    assert "telegram_bot_id" in {f.name for f in fields(AuditEvent)}
    event = ActivityEvent(telegram_bot_id=7_000_000_001, action_type="X", telegram_user_id=1)
    assert any(str(p).startswith("bots/bot_7000000001/") for p in event_paths(event))
