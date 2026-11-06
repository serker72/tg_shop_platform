"""Object storage contract shared by every module.

The protocol is framework-free and dependency-free: business code depends on
:class:`StorageService` only, while concrete backends (MinIO, AWS S3, an in-memory fake in
tests) live in ``app.shared_kernel.infrastructure.storage``. Switching ``minio`` -> ``s3``
therefore never touches a single caller.

Keys are POSIX-like paths without a leading slash (``audit/audit_logs_p2026_01/data.csv``).
Implementations must not interpret them beyond the object name rules of the backend.

Errors raised by implementations are documented per method; every failure surfaces as a
:class:`~app.shared_kernel.domain.exceptions.StorageError` subclass.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import BinaryIO, Protocol, runtime_checkable

from app.shared_kernel.domain.exceptions import StorageError

__all__ = [
    "StorageConflictError",
    "StorageObjectNotFoundError",
    "StorageService",
    "StorageUnavailableError",
    "StoredObject",
]


class StorageObjectNotFoundError(StorageError):
    """The requested object does not exist in the bucket."""


class StorageConflictError(StorageError):
    """The object changed concurrently between two steps of an operation.

    A retryable race, not a backend failure: the caller may repeat the operation (e.g.
    ``copy`` with ``code="storage_source_changed"`` when the source was overwritten).
    """


class StorageUnavailableError(StorageError):
    """The storage backend is unreachable or rejected the request.

    Wraps the original client error as ``__cause__`` so callers can log it without
    depending on the concrete SDK.
    """


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Metadata of an object stored in the bucket.

    Args:
        key: object key (path inside the bucket).
        size: size in bytes.
        etag: backend-specific entity tag (hex MD5 for a plain upload, quoted string);
            ``None`` when the backend does not report it.
        content_type: MIME type the object was stored with, if known.
    """

    key: str
    size: int
    etag: str | None = None
    content_type: str | None = None


@runtime_checkable
class StorageService(Protocol):
    """S3-compatible object storage abstraction.

    All methods are coroutines: implementations offload the blocking SDK call to a worker
    thread, so the event loop is never blocked.
    """

    async def save(
        self,
        key: str,
        data: bytes | BinaryIO,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        """Store ``data`` under ``key``, replacing the previous content if any.

        ``data`` is either the whole payload as ``bytes`` or a binary stream; streams are
        uploaded in chunks and never fully read into memory. A stream must be seekable and
        positioned at 0: the S3 SDK (botocore) rewinds the body with ``seek(0)`` before a
        retry, so a stream starting mid-way would silently upload its prefix as well.

        Raises:
            StorageError: ``code="storage_stream_not_seekable"`` or
                ``code="storage_stream_not_at_start"`` (checked before any backend call).
            StorageUnavailableError: the backend rejected the request.
        """
        ...

    async def get(self, key: str) -> bytes:
        """Return the whole object body.

        Raises:
            StorageObjectNotFoundError: the object does not exist.
            StorageUnavailableError: the backend rejected the request.
        """
        ...

    def get_stream(self, key: str) -> AsyncIterator[bytes]:
        """Iterate over the object body in chunks.

        Used for large payloads (audit partition archives) where loading the whole object
        into memory is unacceptable.

        Raises:
            StorageObjectNotFoundError: the object does not exist.
            StorageUnavailableError: the backend rejected the request.
        """
        ...

    async def delete(self, key: str) -> bool:
        """Delete the object.

        Returns:
            ``True`` if the object existed and was deleted, ``False`` if it was absent.
            Deleting a missing key is not an error (idempotent delete).

        Raises:
            StorageUnavailableError: the backend rejected the request.
        """
        ...

    async def copy(self, source_key: str, target_key: str) -> StoredObject:
        """Server-side copy of ``source_key`` to ``target_key``.

        Raises:
            StorageObjectNotFoundError: the source object does not exist.
            StorageConflictError: ``code="storage_source_changed"`` - the source was
                overwritten concurrently; nothing was copied, the call may be retried.
            StorageUnavailableError: the backend rejected the request.
        """
        ...

    async def exists(self, key: str) -> bool:
        """Return ``True`` if the object exists.

        Raises:
            StorageUnavailableError: the backend rejected the request.
        """
        ...
