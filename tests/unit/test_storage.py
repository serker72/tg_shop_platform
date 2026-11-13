"""Unit tests for the object storage layer (:mod:`app.shared_kernel.*.storage`)."""

from __future__ import annotations

import asyncio
import gc
import io
import os
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from app.core.config import Settings
from app.shared_kernel.domain.exceptions import StorageError
from app.shared_kernel.domain.storage import (
    StorageConflictError,
    StorageObjectNotFoundError,
    StorageService,
    StorageUnavailableError,
    StoredObject,
)
from app.shared_kernel.infrastructure.storage.boto3_storage import (
    MAX_POOL_CONNECTIONS,
    Boto3StorageService,
)
from app.shared_kernel.infrastructure.storage.minio import MinIOStorage
from app.shared_kernel.infrastructure.storage.s3 import S3Storage

pytestmark = pytest.mark.req("R5")

BUCKET = "tg-shop"


def client_error(code: str, operation: str = "HeadObject") -> ClientError:
    """Build a ``ClientError`` as the SDK reports it."""
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, operation)


@pytest.fixture(autouse=True)
def _isolated_storage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings here come only from defaults and explicit values: drop ``MINIO_*`` /
    ``STORAGE_*`` of the process (e.g. after ``set -a; . ./.env.test``)."""
    for name in list(os.environ):
        if name.startswith(("MINIO_", "STORAGE_")):
            monkeypatch.delenv(name)


@pytest.fixture
def settings() -> Settings:
    """Settings with a fixed bucket, built without any dotenv file."""
    return Settings(_env_file=None, storage={"bucket": BUCKET})


@pytest.fixture
def client() -> Any:
    """MagicMock standing in for a ``boto3`` S3 client."""
    return MagicMock()


@pytest.fixture
def storage(client: Any) -> Boto3StorageService:
    """The base implementation under test."""
    return Boto3StorageService(client=client, bucket=BUCKET, endpoint_url="http://minio:9000")


class _FakeBody:
    """Minimal stand-in for ``botocore.response.StreamingBody``."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self) -> None:
        self.closed = True


class TestProtocolConformance:
    def test_minio_storage_satisfies_protocol(self, client: Any, settings: Settings) -> None:
        assert isinstance(MinIOStorage(client=client, settings=settings), StorageService)

    def test_s3_storage_satisfies_protocol(self, client: Any, settings: Settings) -> None:
        assert isinstance(S3Storage(client=client, settings=settings), StorageService)

    def test_base_implementation_satisfies_protocol(self, storage: Boto3StorageService) -> None:
        assert isinstance(storage, StorageService)

    def test_backend_bucket_and_endpoint(self, client: Any, settings: Settings) -> None:
        minio = MinIOStorage(client=client, settings=settings)

        assert minio.bucket == BUCKET
        assert minio.endpoint_url == "http://minio:9000"
        assert "MinIOStorage" in repr(minio)


class TestSave:
    async def test_bytes_payload_passes_put_object_arguments(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.put_object.return_value = {"ETag": '"abc123"'}

        result = await storage.save(
            "audit/x.csv",
            b"payload",
            content_type="text/csv",
            metadata={"rows": "3"},
        )

        client.put_object.assert_called_once_with(
            Bucket=BUCKET,
            Key="audit/x.csv",
            Body=b"payload",
            ContentType="text/csv",
            Metadata={"rows": "3"},
        )
        assert result == StoredObject(
            key="audit/x.csv", size=7, etag="abc123", content_type="text/csv"
        )

    async def test_optional_arguments_are_omitted(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.put_object.return_value = {"ETag": '"e"'}

        result = await storage.save("k", b"12345")

        client.put_object.assert_called_once_with(Bucket=BUCKET, Key="k", Body=b"12345")
        assert result.size == 5
        assert result.content_type is None

    async def test_stream_payload_is_uploaded_as_is(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.put_object.return_value = {"ETag": '"s"'}
        stream = io.BytesIO(b"x" * 42)

        result = await storage.save("audit/stream.csv", stream, content_type="text/csv")

        # The very same stream object reaches the SDK: nothing is read into memory first.
        assert client.put_object.call_args.kwargs["Body"] is stream
        assert result.size == 42
        assert result.etag == "s"
        client.head_object.assert_not_called()

    async def test_stream_not_at_start_is_rejected_before_upload(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        stream = io.BytesIO(b"0123456789")
        stream.seek(5)

        with pytest.raises(StorageError) as excinfo:
            await storage.save("k", stream)

        assert excinfo.value.code == "storage_stream_not_at_start"
        client.put_object.assert_not_called()

    async def test_stream_at_start_has_full_size_and_reaches_sdk_at_zero(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        positions: list[int] = []

        def fake_put(**kwargs: Any) -> dict[str, str]:
            positions.append(kwargs["Body"].tell())
            return {"ETag": '"s"'}

        client.put_object.side_effect = fake_put
        stream = io.BytesIO(b"0123456789")

        result = await storage.save("k", stream)

        assert result.size == 10
        assert positions == [0]
        client.head_object.assert_not_called()

    async def test_non_seekable_stream_is_rejected_before_upload(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        class _Pipe(io.RawIOBase):
            def readable(self) -> bool:
                return True

            def seekable(self) -> bool:
                return False

        with pytest.raises(StorageError) as excinfo:
            await storage.save("k", _Pipe())  # type: ignore[arg-type]

        assert excinfo.value.code == "storage_stream_not_seekable"
        client.put_object.assert_not_called()

    async def test_missing_bucket_is_unavailable(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.put_object.side_effect = client_error("NoSuchBucket", "PutObject")

        with pytest.raises(StorageUnavailableError) as excinfo:
            await storage.save("k", b"data")

        assert excinfo.value.code == "storage_bucket_missing"

    async def test_programming_error_is_not_translated(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.put_object.side_effect = TypeError("bad argument")

        with pytest.raises(TypeError, match="bad argument"):
            await storage.save("k", b"data")

    async def test_backend_error_becomes_unavailable(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.put_object.side_effect = client_error("AccessDenied", "PutObject")

        with pytest.raises(StorageUnavailableError) as excinfo:
            await storage.save("k", b"data")

        assert excinfo.value.code == "storage_unavailable"
        assert isinstance(excinfo.value.__cause__, ClientError)


class TestGet:
    async def test_returns_body_bytes(self, storage: Boto3StorageService, client: Any) -> None:
        client.get_object.return_value = {"Body": _FakeBody([b"hello world"])}

        assert await storage.get("k") == b"hello world"
        client.get_object.assert_called_once_with(Bucket=BUCKET, Key="k")

    async def test_missing_key_maps_to_not_found(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.get_object.side_effect = client_error("NoSuchKey", "GetObject")

        with pytest.raises(StorageObjectNotFoundError):
            await storage.get("missing")

    async def test_http_404_maps_to_not_found(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.get_object.side_effect = client_error("404", "GetObject")

        with pytest.raises(StorageObjectNotFoundError):
            await storage.get("missing")

    async def test_missing_bucket_is_unavailable(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.get_object.side_effect = client_error("NoSuchBucket", "GetObject")

        with pytest.raises(StorageUnavailableError) as excinfo:
            await storage.get("k")

        assert excinfo.value.code == "storage_bucket_missing"

    async def test_type_error_propagates_as_is(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.get_object.side_effect = TypeError("boom")

        with pytest.raises(TypeError):
            await storage.get("k")


class TestGetStream:
    async def test_yields_chunks_and_closes_body(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        body = _FakeBody([b"aa", b"bb", b"cc"])
        client.get_object.return_value = {"Body": body}

        chunks = [chunk async for chunk in storage.get_stream("k")]

        assert chunks == [b"aa", b"bb", b"cc"]
        assert body.closed is True

    async def test_early_break_closes_body(self, storage: Boto3StorageService, client: Any) -> None:
        body = _FakeBody([b"aa", b"bb"])
        client.get_object.return_value = {"Body": body}

        stream = storage.get_stream("k")
        async for _chunk in stream:
            break
        # ``break`` only suspends the generator: each iteration level must be closed
        # explicitly (the context managers of the pipeline do it in real code).
        aclose = getattr(stream, "aclose", None)
        assert aclose is not None
        await aclose()

        assert body.closed is True

    async def test_missing_key_maps_to_not_found(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.get_object.side_effect = client_error("NoSuchKey", "GetObject")

        with pytest.raises(StorageObjectNotFoundError):
            async for _chunk in storage.get_stream("missing"):
                pass

    async def test_cancel_waits_for_read_before_close(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        events: list[str] = []
        started = threading.Event()
        release = threading.Event()

        class _SlowBody:
            def read(self, size: int = -1) -> bytes:
                events.append("read_start")
                started.set()
                release.wait(timeout=5)
                events.append("read_end")
                return b"chunk"

            def close(self) -> None:
                events.append("close")

        client.get_object.return_value = {"Body": _SlowBody()}

        async def consume() -> None:
            async for _chunk in storage.get_stream("k"):
                pass

        task = asyncio.create_task(consume())
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        asyncio.get_running_loop().call_later(0.05, release.set)

        with pytest.raises(asyncio.CancelledError):
            await task

        assert events == ["read_start", "read_end", "close"]

    @pytest.mark.parametrize("read_fails", [False, True])
    async def test_double_cancel_waits_for_read_and_retrieves_outcome(
        self, storage: Boto3StorageService, client: Any, read_fails: bool
    ) -> None:
        events: list[str] = []
        started = threading.Event()
        release = threading.Event()

        class _SlowBody:
            def read(self, size: int = -1) -> bytes:
                events.append("read_start")
                started.set()
                release.wait(timeout=5)
                events.append("read_end")
                if read_fails:
                    raise OSError("connection reset")
                return b"chunk"

            def close(self) -> None:
                events.append("close")

        client.get_object.return_value = {"Body": _SlowBody()}
        loop = asyncio.get_running_loop()
        unhandled: list[dict[str, Any]] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

        async def consume() -> None:
            async for _chunk in storage.get_stream("k"):
                pass

        try:
            task = asyncio.create_task(consume())
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0.02)  # the generator is now waiting for the read
            task.cancel()
            await asyncio.sleep(0.02)
            assert events == ["read_start"]
            release.set()

            with pytest.raises(asyncio.CancelledError):
                await task
            del task
            gc.collect()
            await asyncio.sleep(0)
        finally:
            release.set()
            loop.set_exception_handler(previous_handler)

        assert events == ["read_start", "read_end", "close"]
        assert unhandled == []


class TestExists:
    async def test_true_when_head_object_succeeds(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.head_object.return_value = {"ContentLength": 1}

        assert await storage.exists("k") is True

    @pytest.mark.parametrize("code", ["404", "NoSuchKey"])
    async def test_false_when_object_is_absent(
        self, storage: Boto3StorageService, client: Any, code: str
    ) -> None:
        client.head_object.side_effect = client_error(code)

        assert await storage.exists("k") is False
        client.head_bucket.assert_called_once_with(Bucket=BUCKET)

    async def test_missing_bucket_is_unavailable(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.head_object.side_effect = client_error("404")
        client.head_bucket.side_effect = client_error("404", "HeadBucket")

        with pytest.raises(StorageUnavailableError) as excinfo:
            await storage.exists("k")

        assert excinfo.value.code == "storage_bucket_missing"

    async def test_type_error_propagates_as_is(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.head_object.side_effect = TypeError("boom")

        with pytest.raises(TypeError):
            await storage.exists("k")

    @pytest.mark.parametrize("code", ["500", "SlowDown", "InternalError"])
    async def test_backend_failure_is_unavailable(
        self, storage: Boto3StorageService, client: Any, code: str
    ) -> None:
        client.head_object.side_effect = client_error(code)

        with pytest.raises(StorageUnavailableError) as excinfo:
            await storage.exists("k")

        assert isinstance(excinfo.value.__cause__, ClientError)


class TestDelete:
    async def test_true_when_object_existed(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.head_object.return_value = {"ContentLength": 1}

        assert await storage.delete("k") is True
        client.delete_object.assert_called_once_with(Bucket=BUCKET, Key="k")

    async def test_false_when_object_absent(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.head_object.side_effect = client_error("NoSuchKey")

        assert await storage.delete("k") is False
        client.delete_object.assert_not_called()

    async def test_missing_bucket_is_unavailable(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.head_object.side_effect = client_error("404")
        client.head_bucket.side_effect = client_error("404", "HeadBucket")

        with pytest.raises(StorageUnavailableError) as excinfo:
            await storage.delete("k")

        assert excinfo.value.code == "storage_bucket_missing"
        client.delete_object.assert_not_called()

    async def test_backend_failure_is_unavailable(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.head_object.side_effect = client_error("SlowDown")

        with pytest.raises(StorageUnavailableError):
            await storage.delete("k")


class TestCopy:
    async def test_metadata_comes_from_source_head_and_copy_response(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.head_object.return_value = {
            "ContentLength": 9,
            "ContentType": "text/csv",
            "ETag": '"src-etag"',
        }
        client.copy_object.return_value = {"CopyObjectResult": {"ETag": '"copy-etag"'}}

        result = await storage.copy("src.csv", "dst.csv")

        client.head_object.assert_called_once_with(Bucket=BUCKET, Key="src.csv")
        client.copy_object.assert_called_once_with(
            Bucket=BUCKET,
            Key="dst.csv",
            CopySource={"Bucket": BUCKET, "Key": "src.csv"},
            CopySourceIfMatch='"src-etag"',
        )
        assert result == StoredObject(
            key="dst.csv", size=9, etag="copy-etag", content_type="text/csv"
        )

    async def test_missing_source_is_not_found_without_copy(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.head_object.side_effect = client_error("404")

        with pytest.raises(StorageObjectNotFoundError):
            await storage.copy("src.csv", "dst.csv")

        client.copy_object.assert_not_called()
        client.head_bucket.assert_called_once_with(Bucket=BUCKET)

    async def test_missing_bucket_is_unavailable(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.head_object.side_effect = client_error("404")
        client.head_bucket.side_effect = client_error("404", "HeadBucket")

        with pytest.raises(StorageUnavailableError) as excinfo:
            await storage.copy("src.csv", "dst.csv")

        assert excinfo.value.code == "storage_bucket_missing"
        client.copy_object.assert_not_called()

    @pytest.mark.parametrize("code", ["PreconditionFailed", "412"])
    async def test_source_changed_during_copy_is_conflict(
        self, storage: Boto3StorageService, client: Any, code: str
    ) -> None:
        client.head_object.return_value = {"ContentLength": 1, "ETag": '"v1"'}
        client.copy_object.side_effect = client_error(code, "CopyObject")

        with pytest.raises(StorageConflictError) as excinfo:
            await storage.copy("src.csv", "dst.csv")

        assert excinfo.value.code == "storage_source_changed"
        assert not isinstance(excinfo.value, StorageUnavailableError)
        assert isinstance(excinfo.value.__cause__, ClientError)
        assert "src.csv" in str(excinfo.value)
        assert BUCKET in str(excinfo.value)

    async def test_http_412_status_without_code_is_conflict(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.head_object.return_value = {"ContentLength": 1, "ETag": '"v1"'}
        client.copy_object.side_effect = ClientError(
            {"Error": {}, "ResponseMetadata": {"HTTPStatusCode": 412}}, "CopyObject"
        )

        with pytest.raises(StorageConflictError) as excinfo:
            await storage.copy("src.csv", "dst.csv")

        assert excinfo.value.code == "storage_source_changed"

    async def test_source_removed_between_head_and_copy_maps_to_not_found(
        self, storage: Boto3StorageService, client: Any
    ) -> None:
        client.head_object.return_value = {"ContentLength": 1}
        client.copy_object.side_effect = client_error("NoSuchKey", "CopyObject")

        with pytest.raises(StorageObjectNotFoundError):
            await storage.copy("src.csv", "dst.csv")


class TestClientConfiguration:
    def test_minio_client_is_path_style_and_uses_endpoint(self, monkeypatch: Any) -> None:
        captured: dict[str, Any] = {}

        def fake_client(service: str, **kwargs: Any) -> Any:
            captured["service"] = service
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr("boto3.client", fake_client)
        settings = Settings(_env_file=None)

        from app.shared_kernel.infrastructure.storage.minio import build_minio_client

        build_minio_client(settings)

        assert captured["service"] == "s3"
        assert captured["endpoint_url"] == "http://minio:9000"
        assert captured["config"].s3 == {"addressing_style": "path"}
        assert captured["config"].signature_version == "s3v4"
        assert captured["config"].retries["mode"] == "standard"
        assert captured["config"].retries["max_attempts"] == 3

    def test_s3_client_is_virtual_style(self, monkeypatch: Any) -> None:
        captured: dict[str, Any] = {}

        def fake_client(service: str, **kwargs: Any) -> Any:
            captured["service"] = service
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr("boto3.client", fake_client)
        settings = Settings(_env_file=None, storage={"backend": "s3", "region": "eu-central-1"})

        from app.shared_kernel.infrastructure.storage.s3 import build_s3_client

        build_s3_client(settings)

        assert captured["endpoint_url"] is None
        assert captured["region_name"] == "eu-central-1"
        assert captured["config"].s3 == {"addressing_style": "virtual"}
        assert captured["config"].max_pool_connections == MAX_POOL_CONNECTIONS

    def test_s3_client_uses_storage_credentials_only(self, monkeypatch: Any) -> None:
        captured: dict[str, Any] = {}

        def fake_client(service: str, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr("boto3.client", fake_client)
        settings = Settings(
            _env_file=None,
            storage={"backend": "s3", "access_key": "AKIA", "secret_key": "s3-secret"},
        )

        from app.shared_kernel.infrastructure.storage.s3 import build_s3_client

        build_s3_client(settings)

        assert captured["aws_access_key_id"] == "AKIA"
        assert captured["aws_secret_access_key"] == "s3-secret"


class _FakeClient:
    """Stand-in for a ``boto3`` client: records ``close()`` calls."""

    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class TestStorageProvider:
    @pytest.fixture
    def fake_clients(self, monkeypatch: pytest.MonkeyPatch) -> list[_FakeClient]:
        created: list[_FakeClient] = []

        def fake_client(service: str, **kwargs: Any) -> _FakeClient:
            client = _FakeClient(kwargs)
            created.append(client)
            return client

        monkeypatch.setattr("boto3.client", fake_client)
        return created

    @pytest.mark.parametrize(("backend", "expected"), [("minio", MinIOStorage), ("s3", S3Storage)])
    def test_backend_selection_and_client_lifecycle(
        self, fake_clients: list[_FakeClient], backend: str, expected: type[Any]
    ) -> None:
        settings = Settings(_env_file=None, storage={"backend": backend, "bucket": BUCKET})
        container = self._container(settings)

        storage = container.get(StorageService)

        assert isinstance(storage, expected)
        assert len(fake_clients) == 1
        fake = fake_clients[0]
        assert fake.kwargs["config"].max_pool_connections == MAX_POOL_CONNECTIONS
        assert fake.close_calls == 0
        container.close()
        assert fake.close_calls == 1

    def test_s3_without_storage_credentials_uses_boto3_chain(
        self, fake_clients: list[_FakeClient]
    ) -> None:
        settings = Settings(
            _env_file=None,
            storage={"backend": "s3", "access_key": "", "secret_key": ""},
        )
        container = self._container(settings)

        container.get(StorageService)

        kwargs = fake_clients[0].kwargs
        assert "aws_access_key_id" in kwargs
        assert kwargs["aws_access_key_id"] is None
        assert kwargs["aws_secret_access_key"] is None
        container.close()

    def _container(self, settings: Settings) -> Any:
        from dishka import make_container

        from app.shared_kernel.infrastructure.providers import StorageProvider

        return make_container(StorageProvider(), context={Settings: settings})

    @pytest.mark.parametrize("backend", ["minio", "s3"])
    def test_audit_archive_storage_shares_client(
        self, fake_clients: list[_FakeClient], backend: str
    ) -> None:
        from app.shared_kernel.infrastructure.providers import AuditArchiveStorage

        settings = Settings(
            _env_file=None,
            storage={"backend": backend, "bucket": BUCKET, "audit_archive_bucket": "arch-s3"},
            minio={"audit_archive_bucket": "arch-minio"},
        )
        container = self._container(settings)

        main = container.get(StorageService)
        archive = container.get(AuditArchiveStorage)

        assert archive is not main
        assert isinstance(archive, type(main))
        assert main.bucket == BUCKET
        assert archive.bucket == ("arch-minio" if backend == "minio" else "arch-s3")
        assert len(fake_clients) == 1
        container.close()
        assert fake_clients[0].close_calls == 1

    def test_audit_archive_storage_falls_back_to_main_bucket(
        self, fake_clients: list[_FakeClient]
    ) -> None:
        from app.shared_kernel.infrastructure.providers import AuditArchiveStorage

        settings = Settings(
            _env_file=None, storage={"bucket": BUCKET}, minio={"audit_archive_bucket": ""}
        )
        container = self._container(settings)

        assert container.get(AuditArchiveStorage).bucket == BUCKET
        container.close()

    def test_minio_backend_is_default(self) -> None:
        settings = Settings(_env_file=None, storage={"bucket": BUCKET})

        container = self._container(settings)

        assert isinstance(container.get(StorageService), MinIOStorage)
        container.close()

    def test_s3_backend_is_selected_by_config(self) -> None:
        settings = Settings(_env_file=None, storage={"backend": "s3", "bucket": BUCKET})

        container = self._container(settings)

        assert isinstance(container.get(StorageService), S3Storage)
        container.close()
