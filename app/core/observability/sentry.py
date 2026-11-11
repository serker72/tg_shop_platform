"""Sentry SDK (GlitchTip-compatible) initialisation with secret scrubbing.

Logging integration: structlog renders through the stdlib root logger, so Sentry's default
``LoggingIntegration`` sees every record exactly once; records ``>= ERROR`` become events,
lower levels are not even kept as breadcrumbs (they may carry payloads).
"""

from __future__ import annotations

import logging
import re
from typing import Any, cast

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.types import Event, Hint

from app.core.config import Settings
from app.core.version import PROJECT_SLUG, get_version

__all__ = ["FILTERED", "before_send", "init_sentry", "is_secret_key", "resolve_release", "scrub"]

FILTERED = "[Filtered]"

# Matched against whole *segments* of a key (split on ``_``, ``-``, ``.``, whitespace and
# camelCase boundaries), so ``author``/``compass``/``tokens_count`` stay visible while
# ``password``/``bot_token``/``X-Api-Key``/``SENTRY_DSN`` are masked.
_SECRET_SEGMENTS: frozenset[str] = frozenset(
    {
        "pass",
        "passwd",
        "password",
        "pwd",
        "secret",
        "token",
        "dsn",
        "auth",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "fernet",
        "apikey",
        "session",
    }
)
# Two-segment secrets: ``api_key``, ``access_key``, ``private_key``, ``encryption_key``.
_SECRET_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {("api", "key"), ("access", "key"), ("private", "key"), ("encryption", "key")}
)
_SEGMENT_SPLIT_RE = re.compile(r"[_\-.\s]+|(?<=[a-z0-9])(?=[A-Z])")
# ``scheme://user:password@`` -> ``scheme://user:[Filtered]@``.
_URL_CREDENTIALS_RE = re.compile(r"(?P<head>[a-zA-Z][a-zA-Z0-9+.\-]*://[^:/@\s]*:)[^@\s]+@")
# Telegram bot token: ``<digits>:<35 chars>``.
# No ``\b``: in ``/bot123:ABC.../getMe`` the digits follow a letter. The trailing ``\-``/``_``
# class is greedy, so the whole token is consumed.
_BOT_TOKEN_RE = re.compile(r"(?<!\d)\d{5,}:[A-Za-z0-9_\-]{30,}")


def is_secret_key(key: str) -> bool:
    """Return ``True`` when one of the key's segments names a secret."""
    segments = [s.lower() for s in _SEGMENT_SPLIT_RE.split(key) if s]
    if any(segment in _SECRET_SEGMENTS for segment in segments):
        return True
    return any(pair in _SECRET_PAIRS for pair in zip(segments, segments[1:], strict=False))


def _scrub_text(value: str) -> str:
    value = _URL_CREDENTIALS_RE.sub(lambda m: f"{m.group('head')}{FILTERED}@", value)
    return _BOT_TOKEN_RE.sub(FILTERED, value)


def scrub(value: Any) -> Any:
    """Recursively mask secret-looking keys and credentials embedded in strings."""
    if isinstance(value, dict):
        return {
            key: FILTERED
            if isinstance(key, str) and is_secret_key(key) and value[key] is not None
            else scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def before_send(event: Event, hint: Hint) -> Event | None:
    """``before_send`` hook: scrub the whole event payload."""
    return cast(Event, scrub(event))


def resolve_release(settings: Settings) -> str:
    """``SENTRY_RELEASE`` if set, otherwise ``tg-shop-platform@<version>``."""
    return settings.observability.sentry_release or f"{PROJECT_SLUG}@{get_version()}"


def init_sentry(settings: Settings, *, component: str) -> bool:
    """Initialise the SDK; an empty ``SENTRY_DSN`` is a no-op. Return whether enabled."""
    obs = settings.observability
    if obs.sentry_dsn is None:
        return False
    sentry_sdk.init(
        dsn=obs.sentry_dsn.get_secret_value(),
        release=resolve_release(settings),
        environment=obs.sentry_environment or settings.project_environment,
        traces_sample_rate=obs.sentry_traces_sample_rate,
        send_default_pii=False,
        before_send=before_send,
        # Transactions carry span descriptions / URLs (e.g. ``/bot<token>/getMe``).
        before_send_transaction=before_send,
        integrations=[LoggingIntegration(level=None, event_level=logging.ERROR)],
    )
    sentry_sdk.set_tag("component", component)
    return True
