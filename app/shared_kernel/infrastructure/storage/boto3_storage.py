"""``StorageService`` implementation on top of the official ``boto3`` S3 client.

``boto3`` is synchronous and has no official async API, so every SDK call is executed in a
worker thread via :func:`asyncio.to_thread`; the event loop is never blocked. Blocks of
:meth:`Boto3StorageService.get_stream` are also fetched one by one in a thread, so a large
object is streamed with bounded memory.

The backend differences (MinIO vs AWS S3) are limited to the client configuration and live
in the subclasses; all behaviour is implemented here.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any, BinaryIO

# ``botocore`` ships no type stubs (no ``py.typed``); the exception classes are typed ``Any``.
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from app.core.logging import get_logger
from app.shared_kernel.domain.exceptions import StorageError
from app.shared_kernel.domain.storage import (
    StorageConflictError,
    StorageObjectNotFoundError,
    StorageService,
    StorageUnavailableError,
    StoredObject,
)

__all__ = ["Boto3StorageService", "CHUNK_SIZE", "MAX_POOL_CONNECTIONS"]

logger = get_logger(__name__)

# 8 MiB: large enough to keep the S3 round-trips low, small enough to keep memory bounded.
CHUNK_SIZE = 8 * 1024 * 1024

# urllib3 pool size of the client: >= the default ``asyncio.to_thread`` executor size
# (``min(32, cpu + 4)``), so concurrent SDK calls never wait for a free connection.
MAX_POOL_CONNECTIONS = 32

# Only SDK errors are translated into the domain hierarchy: ``ClientError`` (an S3 error
# response) and ``BotoCoreError`` (network, endpoint, credentials). Programming errors
# (``TypeError``, ``AttributeError``, ...) propagate unchanged.
_SDK_ERRORS: tuple[type[BaseException], ...] = (ClientError, BotoCoreError)

# S3 error codes meaning "there is no object under this key". ``HeadObject`` has no response
# body, so a missing bucket is reported there as a bare ``404`` too; ``exists``/``delete``
# disambiguate it with ``HeadBucket``.
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})

# Code raised when the bucket itself is absent: always ``StorageUnavailableError``.
_NO_SUCH_BUCKET = "NoSuchBucket"

# A failed conditional request (``CopySourceIfMatch``): the source changed concurrently.
_PRECONDITION_FAILED_CODES = frozenset({"PreconditionFailed", "412"})


def _is_precondition_failed(error: object) -> bool:
    """Return ``True`` for ``PreconditionFailed`` / HTTP 412 client errors."""
    if _error_code(error) in _PRECONDITION_FAILED_CODES:
        return True
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    status: object = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status == 412


def _error_code(error: object) -> str:
    """Extract the S3 error code from a ``ClientError``-like object."""
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return ""
    code = response.get("Error", {}).get("Code")
    return str(code) if code is not None else ""


def _is_not_found(error: object) -> bool:
    """Return ``True`` when the client error means "object does not exist"."""
    return _error_code(error) in _NOT_FOUND_CODES


def _clean_etag(etag: object) -> str | None:
    """Return the ETag without the surrounding quotes S3 adds."""
    if not isinstance(etag, str):
        return None
    return etag.strip('"')


def _stream_size(stream: BinaryIO) -> int:
    """Return the size of ``stream``, which must be seekable and positioned at 0.

    botocore rewinds the request body with ``seek(0)`` before every retry
    (``AWSRequest.reset_stream``), not to the original position: a stream starting mid-way
    would upload its prefix on a retry while the reported size covers only the remainder.
    Hence the strict "position 0" contract. The stream is left at position 0.
    """
    seekable = getattr(stream, "seekable", None)
    if seekable is None or not seekable():
        raise StorageError(
            "Stream payload must be seekable: its size is required for PutObject",
            code="storage_stream_not_seekable",
        )
    position = stream.tell()
    if position != 0:
        raise StorageError(
            f"Stream payload must be positioned at 0, got {position}: botocore retries "
            "rewind the body to 0",
            code="storage_stream_not_at_start",
        )
    size = stream.seek(0, os.SEEK_END)
    stream.seek(0)
    return size


class Boto3StorageService(StorageService):
    """S3-compatible storage backed by a synchronous ``boto3`` client.

    Uploads and copies use single ``PutObject``/``CopyObject`` requests, limited by S3 to
    5 GB per object. Multipart transfers (``upload_fileobj``/``copy`` of
    ``boto3.s3.transfer``) are a future task for archiving large audit partitions.

    Args:
        client: ready ``boto3`` S3 client (``Any``: ``botocore`` ships no type stubs, so a
            precise annotation would be a lie and would require cast gymnastics).
        bucket: target bucket; the bucket itself is expected to exist. A missing bucket is
            reported as ``StorageUnavailableError(code="storage_bucket_missing")``.
        endpoint_url: informational copy of the client endpoint, exposed for logging and
            diagnostics only.
    """

    def __init__(self, *, client: Any, bucket: str, endpoint_url: str | None = None) -> None:
        self._client = client
        self._bucket = bucket
        self._endpoint_url = endpoint_url

    @property
    def bucket(self) -> str:
        """Return the configured bucket name."""
        return self._bucket

    @property
    def endpoint_url(self) -> str | None:
        """Return the configured endpoint URL, if any."""
        return self._endpoint_url

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(bucket={self._bucket!r}, endpoint_url={self._endpoint_url!r})"
        )

    async def save(
        self,
        key: str,
        data: bytes | BinaryIO,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        """Upload ``data`` under ``key`` with a single ``PutObject`` request (max 5 GB).

        A ``bytes`` payload goes through ``put_object(Body=...)``; a stream goes through
        ``put_object(Body=stream)`` as well: the SDK reads it in chunks, so the payload is not
        materialised in memory. The stream must be seekable (``storage_stream_not_seekable``)
        and positioned at 0 (``storage_stream_not_at_start``): botocore rewinds the body with
        ``seek(0)`` before a retry, so any other start position would corrupt the object.
        Both are checked before any S3 call. The size is measured up front, so no
        ``HeadObject`` is needed after the upload. Multipart upload (``upload_fileobj``) for
        objects over 5 GB is a future task (audit archive), not implemented here.
        """
        extra: dict[str, Any] = {}
        if content_type is not None:
            extra["ContentType"] = content_type
        if metadata:
            extra["Metadata"] = dict(metadata)
        payload: Any
        if isinstance(data, (bytes, bytearray)):
            payload = bytes(data)
            size = len(payload)
        else:
            size = _stream_size(data)
            payload = data

        def _put() -> Any:
            return self._client.put_object(Bucket=self._bucket, Key=key, Body=payload, **extra)

        try:
            response = await asyncio.to_thread(_put)
        except _SDK_ERRORS as error:
            raise self._translate(error, operation="save", key=key) from error
        return StoredObject(
            key=key,
            size=size,
            etag=_clean_etag(response.get("ETag")) if isinstance(response, dict) else None,
            content_type=content_type,
        )

    async def get(self, key: str) -> bytes:
        """Return the whole object body."""

        def _get() -> bytes:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body: bytes = response["Body"].read()
            return body

        try:
            return await asyncio.to_thread(_get)
        except _SDK_ERRORS as error:
            raise self._translate(error, operation="get", key=key) from error

    async def get_stream(self, key: str) -> AsyncIterator[bytes]:
        """Yield the object body block by block (see :data:`CHUNK_SIZE`).

        The ``StreamingBody`` is opened inside the worker thread and every ``read`` is
        offloaded separately; the body is closed exactly once when iteration stops (early
        ``break`` included). On cancellation the in-flight ``read`` is awaited first, so the
        body is never closed concurrently with a read, then ``CancelledError`` propagates.
        """

        def _open() -> Any:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"]

        def _read_chunk(body: Any) -> bytes:
            chunk: bytes = body.read(CHUNK_SIZE)
            return chunk

        def _close(body: Any) -> None:
            body.close()

        try:
            body = await asyncio.to_thread(_open)
        except _SDK_ERRORS as error:
            raise self._translate(error, operation="get_stream", key=key) from error

        try:
            while True:
                read_task = asyncio.ensure_future(asyncio.to_thread(_read_chunk, body))
                try:
                    chunk = await asyncio.shield(read_task)
                except asyncio.CancelledError:
                    # The thread cannot be interrupted: wait for the read to finish before
                    # ``finally`` closes the body. Repeated cancellations while waiting are
                    # absorbed (the original one is re-raised below), and the read outcome is
                    # retrieved so no "exception was never retrieved" is logged.
                    while not read_task.done():
                        try:
                            await asyncio.wait({read_task})
                        except asyncio.CancelledError:
                            continue
                    if not read_task.cancelled():
                        read_task.exception()
                    raise
                except _SDK_ERRORS as error:
                    raise self._translate(error, operation="get_stream", key=key) from error
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(_close, body)

    async def delete(self, key: str) -> bool:
        """Delete ``key``; return ``False`` when the object was already absent.

        ``DeleteObject`` is idempotent in S3 and returns ``204`` for a missing key, so
        presence is checked with a preceding ``HeadObject`` to honour the ``bool`` result.
        A missing bucket raises ``StorageUnavailableError(code="storage_bucket_missing")``.
        """

        def _delete() -> bool:
            if not self._head_object_exists(key, operation="delete"):
                return False
            self._client.delete_object(Bucket=self._bucket, Key=key)
            return True

        try:
            return await asyncio.to_thread(_delete)
        except _SDK_ERRORS as error:
            raise self._translate(error, operation="delete", key=key) from error

    async def copy(self, source_key: str, target_key: str) -> StoredObject:
        """Server-side copy inside the same bucket with a single ``CopyObject`` (max 5 GB).

        The source is inspected with ``HeadObject`` *before* copying: a missing source raises
        ``StorageObjectNotFoundError`` without calling ``CopyObject``, and size/content type
        come from that head, so no request follows a successful copy (a failing post-copy
        head can no longer turn a done copy into an error). ``CopySourceIfMatch`` pins the
        copied version to the inspected one, so the returned metadata cannot go stale.
        If the source is overwritten between the head and the copy, the backend answers
        ``412 PreconditionFailed`` and ``StorageConflictError(code="storage_source_changed")``
        is raised: nothing was copied and the caller may simply retry.
        Multipart copy (``boto3.s3.transfer``) for larger objects is a future task.

        Raises:
            StorageObjectNotFoundError: the source object does not exist.
            StorageConflictError: the source changed during the copy (retryable).
            StorageUnavailableError: the bucket is missing or the backend failed.
        """

        def _copy() -> StoredObject:
            try:
                head = self._client.head_object(Bucket=self._bucket, Key=source_key)
            except ClientError as error:
                if _is_not_found(error):
                    self._ensure_bucket_exists(operation="copy")
                raise
            extra: dict[str, Any] = {}
            source_etag = head.get("ETag")
            if isinstance(source_etag, str) and source_etag:
                extra["CopySourceIfMatch"] = source_etag
            response = self._client.copy_object(
                Bucket=self._bucket,
                Key=target_key,
                CopySource={"Bucket": self._bucket, "Key": source_key},
                **extra,
            )
            return StoredObject(
                key=target_key,
                size=int(head["ContentLength"]),
                etag=_clean_etag(response.get("CopyObjectResult", {}).get("ETag")),
                content_type=head.get("ContentType"),
            )

        try:
            return await asyncio.to_thread(_copy)
        except _SDK_ERRORS as error:
            if isinstance(error, ClientError) and _is_precondition_failed(error):
                logger.info(
                    "storage_source_changed",
                    bucket=self._bucket,
                    key=source_key,
                    target_key=target_key,
                    operation="copy",
                )
                raise StorageConflictError(
                    f"Source {source_key!r} in bucket {self._bucket!r} changed during copy "
                    f"to {target_key!r}; retry the operation",
                    code="storage_source_changed",
                ) from error
            raise self._translate(error, operation="copy", key=source_key) from error

    async def exists(self, key: str) -> bool:
        """Return ``True`` when ``head_object`` succeeds, ``False`` for a missing object.

        A missing bucket raises ``StorageUnavailableError(code="storage_bucket_missing")``.
        """
        try:
            return await asyncio.to_thread(self._head_object_exists, key, operation="exists")
        except _SDK_ERRORS as error:
            raise self._translate(error, operation="exists", key=key) from error

    def _head_object_exists(self, key: str, *, operation: str) -> bool:
        """Run ``HeadObject`` (in a worker thread); tell a missing object from a missing bucket.

        ``HeadObject`` answers a bare ``404`` both for a missing key and a missing bucket, so a
        not-found answer is confirmed with ``HeadBucket``.
        """
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            if not _is_not_found(error):
                raise
            self._ensure_bucket_exists(operation=operation)
            return False
        return True

    def _ensure_bucket_exists(self, *, operation: str) -> None:
        """Raise ``storage_bucket_missing`` when ``HeadBucket`` reports the bucket as absent."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as error:
            if _is_not_found(error) or _error_code(error) == _NO_SUCH_BUCKET:
                raise self._bucket_missing(operation) from error
            raise

    def _bucket_missing(self, operation: str) -> StorageUnavailableError:
        logger.warning("storage_bucket_missing", bucket=self._bucket, operation=operation)
        return StorageUnavailableError(
            f"Bucket {self._bucket!r} does not exist (operation {operation!r})",
            code="storage_bucket_missing",
        )

    def _translate(self, error: BaseException, *, operation: str, key: str) -> StorageError:
        """Map an SDK exception to the domain hierarchy without leaking SDK types."""
        if isinstance(error, StorageError):
            return error
        code = _error_code(error)
        if code == _NO_SUCH_BUCKET:
            return self._bucket_missing(operation)
        if code in _NOT_FOUND_CODES:
            return StorageObjectNotFoundError(
                f"Object {key!r} was not found in bucket {self._bucket!r}",
                code="storage_object_not_found",
            )
        logger.warning(
            "storage_operation_failed",
            bucket=self._bucket,
            key=key,
            operation=operation,
            error_type=type(error).__name__,
            error=str(error),
        )
        return StorageUnavailableError(
            f"Storage operation {operation!r} on {key!r} failed: {error}",
            code="storage_unavailable",
        )
