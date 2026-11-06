"""Domain error hierarchy.

The module is deliberately framework-free: no SQLAlchemy, FastAPI or aiogram
imports. Entry points translate these errors into transport-specific responses
(HTTP status codes, bot messages, CLI exit codes).
"""

from __future__ import annotations

__all__ = [
    "ConfigurationError",
    "DomainError",
    "EntityAlreadyExistsError",
    "EntityNotFoundError",
    "PermissionDeniedError",
    "StorageError",
    "TenantIsolationError",
    "ValidationError",
]


class DomainError(Exception):
    """Base class for all expected business errors.

    Args:
        message: human-readable description; may be empty when the subtype
            alone conveys the meaning.
        code: stable machine-readable code (``"entity_not_found"``) used by
            entry points for mapping/logging; defaults to ``None``.
    """

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def __repr__(self) -> str:
        return f"{type(self).__name__}(message={self.message!r}, code={self.code!r})"


class EntityNotFoundError(DomainError):
    """Requested entity does not exist or is soft-deleted."""


class EntityAlreadyExistsError(DomainError):
    """Unique constraint violated by the operation."""


class ValidationError(DomainError):
    """Input data breaks a domain invariant."""


class PermissionDeniedError(DomainError):
    """Actor lacks the permission required by the operation."""


class TenantIsolationError(DomainError):
    """Attempt to access an entity belonging to another tenant."""


class StorageError(DomainError):
    """Object storage operation failed (MinIO / S3)."""


class ConfigurationError(DomainError):
    """Runtime configuration is missing or inconsistent."""
