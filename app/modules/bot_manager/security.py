"""Token helpers of the bot manager (no I/O)."""

from __future__ import annotations

import hashlib

__all__ = ["token_hash"]


def token_hash(token: str) -> str:
    """SHA-256 hex digest of a bot token: the lookup key of ``bots.token_hash``.

    The token itself is stored only encrypted (``Bot.encrypted_token``, Fernet with a random
    IV), so equality lookups ("is this token already registered?") go through this hash.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
