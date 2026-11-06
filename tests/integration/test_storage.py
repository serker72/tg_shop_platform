"""Integration tests of ``StorageService`` against a real MinIO instance.

The suite is opt-in (``-m integration``). When MinIO is not reachable it skips with an explicit
reason, unless ``REQUIRE_INFRA=1`` is set (``make test-integration`` sets it after starting the
stack): then the run fails, so missing infrastructure cannot pass as "skipped". Connection
parameters come from ``Settings`` (``.env`` / environment), never from hardcoded values.

Every test works under ``test/<uuid>/`` and removes what it created, including the case of a
failed assertion (best-effort cleanup in ``finally``).
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid
from collections.abc import AsyncIterator

import pytest

from app.core.config import Settings
from app.shared_kernel.domain.storage import (
    StorageObjectNotFoundError,
    StorageService,
    StorageUnavailableError,
)
from app.shared_kernel.infrastructure.storage.minio import MinIOStorage, build_minio_client

pytestmark = pytest.mark.integration

PREFIX = "test"


def _infra_unavailable(reason: str) -> None:
    """Skip, or fail when ``REQUIRE_INFRA=1`` (``make test-integration``) demands the infra."""
    if os.environ.get("REQUIRE_INFRA") == "1":
        pytest.fail(f"REQUIRE_INFRA=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def settings() -> Settings:
    """Settings from the environment / ``.env`` (MinIO is the default backend)."""
    return Settings()


@pytest.fixture(scope="module")
def storage(settings: Settings) -> StorageService:
    """``MinIOStorage`` bound to the configured bucket."""
    if settings.minio.secret_key is None:
        _infra_unavailable("MINIO_SECRET_KEY is not configured: cannot authenticate to MinIO")
    storage = MinIOStorage(client=build_minio_client(settings), settings=settings)
    _ensure_bucket(storage, settings)
    return storage


def _ensure_bucket(storage: StorageService, settings: Settings) -> None:
    """Create the bucket when it is missing (a fresh deployment has no buckets).

    Reaching the backend at all is a prerequisite: an unreachable MinIO stops the module
    here with one reason - skip without ``REQUIRE_INFRA``, fail with ``REQUIRE_INFRA=1``.
    """
    probe_key = f"{PREFIX}/{uuid.uuid4().hex}/probe.bin"
    try:
        stored = asyncio.run(storage.save(probe_key, b"probe"))
        assert stored.size == 5
        asyncio.run(storage.delete(probe_key))
    except StorageUnavailableError as error:  # pragma: no cover - infrastructure dependent
        if error.code != "storage_bucket_missing":
            _infra_unavailable(f"MinIO is not reachable: {error}")
        client = build_minio_client(settings)
        client.create_bucket(Bucket=settings.storage.resolved_bucket(settings.minio))


@pytest.fixture
async def storage_ready(storage: StorageService) -> AsyncIterator[StorageService]:
    """Skip the test with a clear reason when MinIO is unreachable."""
    probe_key = f"{PREFIX}/{uuid.uuid4().hex}/probe.txt"
    try:
        await storage.exists(probe_key)
    except StorageUnavailableError as error:  # pragma: no cover - infrastructure dependent
        _infra_unavailable(f"MinIO is not reachable: {error}")
    yield storage


async def _cleanup(storage: StorageService, created: list[str]) -> None:
    """Remove the keys created by a test, ignoring infrastructure errors."""
    for key in created:
        try:
            await storage.delete(key)
        except StorageUnavailableError:  # pragma: no cover - best effort
            pass


async def test_save_get_exists_copy_delete(storage_ready: StorageService) -> None:
    created: list[str] = []
    run = f"{PREFIX}/{uuid.uuid4().hex}"
    key = f"{run}/payload.bin"
    copy_key = f"{run}/copy.bin"
    payload = b"audit-partition-bytes" * 1024

    try:
        stored = await storage_ready.save(
            key, payload, content_type="application/octet-stream", metadata={"rows": "1024"}
        )
        created.append(key)
        assert stored.key == key
        assert stored.size == len(payload)
        assert stored.content_type == "application/octet-stream"

        assert await storage_ready.exists(key) is True
        assert await storage_ready.get(key) == payload

        streamed = b"".join([chunk async for chunk in storage_ready.get_stream(key)])
        assert streamed == payload

        copied = await storage_ready.copy(key, copy_key)
        created.append(copy_key)
        assert copied.key == copy_key
        assert copied.size == len(payload)
        assert await storage_ready.exists(copy_key) is True
        assert await storage_ready.get(copy_key) == payload

        assert await storage_ready.delete(copy_key) is True
        assert await storage_ready.exists(copy_key) is False

        assert await storage_ready.delete(key) is True
        created.clear()
        assert await storage_ready.exists(key) is False
    finally:
        await _cleanup(storage_ready, created)


async def test_delete_missing_object_returns_false(storage_ready: StorageService) -> None:
    missing = f"{PREFIX}/{uuid.uuid4().hex}/absent.bin"

    assert await storage_ready.delete(missing) is False


async def test_get_missing_object_raises_not_found(storage_ready: StorageService) -> None:
    missing = f"{PREFIX}/{uuid.uuid4().hex}/absent.bin"

    with pytest.raises(StorageObjectNotFoundError):
        await storage_ready.get(missing)
    with pytest.raises(StorageObjectNotFoundError):
        await storage_ready.copy(missing, f"{PREFIX}/{uuid.uuid4().hex}/never.bin")


async def test_save_stream_from_file_like_object(storage_ready: StorageService) -> None:
    created: list[str] = []
    key = f"{PREFIX}/{uuid.uuid4().hex}/stream.csv"
    payload = b"col1,col2\n1,2\n" * 500

    try:
        stored = await storage_ready.save(key, io.BytesIO(payload), content_type="text/csv")
        created.append(key)
        assert stored.size == len(payload)
        assert await storage_ready.get(key) == payload
    finally:
        await _cleanup(storage_ready, created)
