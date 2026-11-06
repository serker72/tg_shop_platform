"""SQLAlchemy ``TypeDecorator`` that transparently encrypts string columns.

The type maps ``str`` Python values to Fernet ciphertext stored in a ``TEXT``
column. Encryption uses the *primary* (first) key, decryption tries every
configured key through :class:`cryptography.fernet.MultiFernet`, which makes
key rotation a two-step, downtime-free procedure:

1. Add the new key in front of the old one (``ENCRYPTION_KEY`` = new token,
   ``ENCRYPTION_KEYS_OLD`` = previous token) and deploy: reads keep working,
   every new write is encrypted with the new key.
2. After a re-encryption pass over the affected rows, drop the old key.

Why ``impl = Text`` and not ``String(n)``
-----------------------------------------
Fernet output is ``base64url(token)``: a 32-byte payload plus a 57-byte header
and the IV/encryption overhead grows with the plaintext. The ciphertext is
therefore *roughly twice* the plaintext length for short values (a 10-character
token already takes 137 bytes) and never shrinks. A ``String(n)`` column sized
after the plaintext would silently truncate on PostgreSQL and produce
undecryptable rows, so the DDL type is unconditional ``TEXT``. The ``length``
argument is accepted for API/DDL compatibility only and has no effect on
storage.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import Text, util
from sqlalchemy.types import TypeDecorator

from app.core.config import get_settings
from app.shared_kernel.domain.exceptions import ConfigurationError, DomainError

__all__ = ["EncryptedString"]


class EncryptedString(TypeDecorator[str]):
    """Encrypt on write, decrypt on read, using Fernet (AES-128-CBC + HMAC).

    Args:
        keys: Fernet keys. The first one is the *primary* key used for
            encryption, the rest are only tried when decrypting (old values).
            When omitted (or empty) the keys are lazily read from
            :attr:`app.core.config.Settings.encryption_key_list`, i.e. from the
            ``ENCRYPTION_KEY`` / ``ENCRYPTION_KEYS_OLD`` environment variables.
        length: Accepted for backwards compatibility with hand-written DDL only.
            It does **not** constrain storage: the column is always ``TEXT``.

    Raises:
        ConfigurationError: when no key is configured, a key is not a valid
            Fernet token, or a stored value cannot be decrypted.

    Notes:
        ``None`` round-trips as SQL ``NULL`` without touching the cipher.
        Plaintext and key material are never logged or embedded in error
        messages.
    """

    cache_ok = True

    #: The Python-side type; the DB-side type is fixed (:attr:`impl`).
    python_type = str

    impl = Text

    def __init__(
        self,
        *,
        keys: Sequence[str] | None = None,
        length: int | None = None,
    ) -> None:
        # ``length`` is deliberately NOT forwarded to ``super().__init__``: it would
        # be applied to the ``TEXT`` impl and produce ``TEXT(n)`` in the DDL, i.e.
        # a narrowed column - exactly what this type must avoid.
        super().__init__()
        self._keys: tuple[str, ...] | None = tuple(keys) if keys else None
        self._length = length
        # Non-secret fingerprint of the explicit key list; ``None`` = keys from settings.
        self._key_fingerprint: str | None = (
            hashlib.sha256("\n".join(self._keys).encode("utf-8")).hexdigest()[:16]
            if self._keys
            else None
        )
        # Built lazily: importing this module must not read settings, and a
        # configured key list is only validated once the column is first used.
        self._cached_fernet: MultiFernet | None = None

    @util.memoized_property
    def _static_cache_key(self) -> tuple[Any, ...]:
        """Distinguish instances by key set without exposing key material.

        The default ``TypeEngine._static_cache_key`` only collects public attributes
        named after ``__init__`` arguments; keys live in ``_keys`` (private, kept out
        of ``repr()`` and therefore out of Alembic autogenerate), so the default key
        would be identical for different key sets.
        """
        return (type(self), self._key_fingerprint)

    def copy(self, **kw: Any) -> EncryptedString:
        """Return a copy without carrying the cached cipher over."""
        instance = super().copy(**kw)
        instance._cached_fernet = None
        return instance

    # -- internals ---------------------------------------------------------

    def _resolve_keys(self) -> tuple[str, ...]:
        """Return the effective key list, falling back to the application settings."""
        if self._keys:
            return self._keys
        keys = tuple(get_settings().encryption_key_list)
        if not keys:
            raise ConfigurationError(
                "No Fernet encryption keys configured: set ENCRYPTION_KEY "
                "(and optionally ENCRYPTION_KEYS_OLD for rotation)",
                code="encryption_key_missing",
            )
        return keys

    def _multi_fernet(self) -> MultiFernet:
        """Build (once per instance) the ``MultiFernet`` wrapper over the key list."""
        if self._cached_fernet is not None:
            return self._cached_fernet
        fernets: list[Fernet] = []
        for index, raw_key in enumerate(self._resolve_keys()):
            try:
                fernets.append(Fernet(raw_key))
            except (ValueError, TypeError) as exc:
                # Never echo the key itself - only its position in the list.
                role = "ENCRYPTION_KEY" if index == 0 else f"ENCRYPTION_KEYS_OLD[{index - 1}]"
                raise ConfigurationError(
                    f"Invalid Fernet key in {role}: expected an urlsafe base64-encoded "
                    "32-byte key (generate one with Fernet.generate_key())",
                    code="encryption_key_invalid",
                ) from exc
        self._cached_fernet = MultiFernet(fernets)
        return self._cached_fernet

    # -- TypeDecorator hooks ----------------------------------------------

    def process_bind_param(self, value: str | None, dialect: Any) -> str | None:
        """Encrypt a Python string with the primary key before it hits the DB."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise DomainError(
                f"EncryptedString expects str or None, got {type(value).__name__}",
                code="encrypted_string_type_error",
            )
        token = self._multi_fernet().encrypt(value.encode("utf-8"))
        return token.decode("ascii")

    def process_result_value(self, value: str | None, dialect: Any) -> str | None:
        """Decrypt a stored value, trying every configured key."""
        if value is None:
            return None
        raw = value.encode("ascii") if isinstance(value, str) else value
        try:
            plaintext = self._multi_fernet().decrypt(raw)
        except InvalidToken as exc:
            raise ConfigurationError(
                "Cannot decrypt stored value: no configured Fernet key matches it "
                "(wrong ENCRYPTION_KEY or a corrupted ciphertext)",
                code="decryption_failed",
            ) from exc
        except (TypeError, ValueError) as exc:
            raise DomainError(
                "Stored value is not valid Fernet ciphertext",
                code="decryption_failed",
            ) from exc
        return plaintext.decode("utf-8")
