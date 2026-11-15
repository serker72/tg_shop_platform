"""Domain events of the logging module (click-log and audit records).

Events are immutable value objects built by the caller; ``created_at`` is captured when the
event object is created (tz-aware UTC), not when the buffer flushes it, so the order of
actions in the DB and in the text files reflects the real time of the action.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from app.modules.logging.models import utc_now

__all__ = ["ActivityEvent", "ActorType", "AuditEvent"]

#: Mirrors ``ACTOR_TYPES`` / DB CHECK ``ck_audit_logs_actor_type``.
ActorType = Literal["user", "system", "bot"]


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("created_at must be a timezone-aware datetime (UTC)")


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """One ``user_activity`` row (TZ 47.1-47.2) without ``id``.

    ``source == "system"`` or a missing ``telegram_user_id`` marks a system event (it is
    additionally written to ``logs/system/<date>.log``).
    """

    telegram_bot_id: int
    action_type: str
    tenant_id: UUID | None = None
    telegram_user_id: int | None = None
    username: str | None = None
    user_type: str | None = None
    role: str | None = None
    source: str | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    order_id: UUID | None = None
    product_id: UUID | None = None
    category_id: UUID | None = None
    subcategory_id: UUID | None = None
    case_id: UUID | None = None
    preorder_id: UUID | None = None
    button: str | None = None
    callback_data: str | None = None
    screen_from: str | None = None
    screen_current: str | None = None
    screen_to: str | None = None
    metadata: Mapping[str, Any] | None = None
    result: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _require_aware(self.created_at)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One ``audit_logs`` row (TZ 47.7) without ``id``."""

    actor_type: ActorType
    action: str
    tenant_id: UUID | None = None
    telegram_bot_id: int | None = None
    actor_id: int | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    payload: Mapping[str, Any] | None = None
    result: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _require_aware(self.created_at)
