"""Token helpers of the bot manager (no I/O)."""

from __future__ import annotations

import hashlib

__all__ = ["token_hash"]


def token_hash(token: str) -> str:
    """SHA-256 hex digest of a bot token: the lookup key of ``bots.token_hash``.

    The token itself is stored only encrypted (``Bot.encrypted_token``, Fernet with a random
    IV), so equality lookups ("is this token already registered?") go through this hash.
    Surrounding whitespace (copy/paste) is stripped first. A plain SHA-256 without a pepper
    is a deliberate choice: Telegram bot tokens are high-entropy secrets (35-char random
    part), a dictionary/brute-force attack on the hash is not practical.
    """
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
