"""Unit tests of :mod:`app.modules.logging.audit_archive` (no infrastructure)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, BinaryIO

import pydantic
import pytest

from app.core.config import Settings
from app.modules.logging import audit_archive as aa
from app.shared_kernel.domain.storage import (
    StorageObjectNotFoundError,
    StorageUnavailableError,
    StoredObject,
)

TABLE = "audit_logs_p20240101"


class FakeStorage:
    """In-memory ``StorageService``; ``tamper`` replaces bytes of a key after ``save``."""

    def __init__(self, tamper: dict[str, bytes] | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.tamper = tamper or {}
        self.saved: list[str] = []

    async def save(
        self,
        key: str,
        data: bytes | BinaryIO,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        body = data if isinstance(data, bytes) else data.read()
        self.objects[key] = self.tamper.get(key, body)
        self.saved.append(key)
        return StoredObject(key=key, size=len(body))

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise StorageObjectNotFoundError(key)
        return self.objects[key]

    async def get_stream(self, key: str) -> AsyncIterator[bytes]:
        body = await self.get(key)
        for start in range(0, len(body), 3):
            yield body[start : start + 3]

    async def delete(self, key: str) -> bool:
        return self.objects.pop(key, None) is not None

    async def copy(self, source_key: str, target_key: str) -> StoredObject:
        raise NotImplementedError

    async def exists(self, key: str) -> bool:
        return key in self.objects


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("audit_logs_p20240101", True),
        ("audit_logs_p2024_01", False),
        ("audit_logs_p20240101_000000", False),
        ("audit_logs_default", False),
        ("user_activity_p20240101", False),
        ("audit_logs_p20240101x", False),
    ],
)
def test_candidate_regex(name: str, expected: bool) -> None:
    assert aa.is_candidate(name) is expected


def test_keys_are_under_audit_prefix() -> None:
    assert aa.data_key(TABLE) == f"audit/{TABLE}/data.csv.gz"
    assert aa.manifest_key(TABLE) == f"audit/{TABLE}/manifest.json"


def test_find_collisions() -> None:
    assert aa.find_collisions(["audit_logs_p20240101", "x"], ["audit_logs_p20240101", "y"]) == [
        "audit_logs_p20240101"
    ]


def _gzip(data: bytes) -> tuple[bytes, str, int]:
    out = io.BytesIO()
    writer = aa.GzipDigestWriter(out)
    writer.write(data[:5])
    writer.write(data[5:])
    sha, size = writer.finish()
    return out.getvalue(), sha, size


def test_gzip_is_deterministic_and_hash_covers_compressed_bytes() -> None:
    payload = b"id,created_at\n1,2024-01-01\n" * 100
    first, sha, size = _gzip(payload)
    second, sha2, _ = _gzip(payload)
    assert first == second
    assert sha == sha2 == hashlib.sha256(first).hexdigest()
    assert size == len(first)
    assert gzip.decompress(first) == payload
    assert first[4:8] == b"\x00\x00\x00\x00"  # mtime = 0


def test_gzip_size_limit() -> None:
    writer = aa.GzipDigestWriter(io.BytesIO(), max_size=30)
    with pytest.raises(aa.ArchiveTooLargeError):
        writer.write(bytes(range(256)) * 50)
        writer.finish()


def test_build_and_parse_manifest() -> None:
    dump = aa.DumpResult(rows=2, sha256="a" * 64, size_bytes=10)
    created = datetime(2024, 1, 1, tzinfo=UTC)
    document = aa.build_manifest(
        table=TABLE,
        dump=dump,
        columns=["id", "created_at"],
        min_created_at=created,
        max_created_at=created,
        archived_at=created,
    )
    assert document["parent"] == "public.audit_logs"
    assert document["format"] == "csv+gzip"
    assert document["columns"] == ["id", "created_at"]
    assert document["min_created_at"] == created.isoformat()
    manifest = aa.parse_manifest(json.dumps(document).encode(), table=TABLE)
    assert manifest == aa.Manifest(TABLE, 2, "a" * 64, 10)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        json.dumps({"table": "other", "schema": "archive", "format": "csv+gzip"}).encode(),
        json.dumps(
            {
                "table": TABLE,
                "schema": "archive",
                "format": "csv+gzip",
                "rows": True,
                "sha256": "a" * 64,
                "size_bytes": 1,
            }
        ).encode(),
    ],
)
def test_parse_manifest_rejects_invalid(raw: bytes) -> None:
    with pytest.raises(aa.ManifestMismatchError):
        aa.parse_manifest(raw, table=TABLE)


async def test_publish_verifies_checksum_before_manifest() -> None:
    body, sha, size = _gzip(b"id\n1\n")
    storage = FakeStorage(tamper={aa.data_key(TABLE): b"corrupted"})
    dump = aa.DumpResult(rows=1, sha256=sha, size_bytes=size)

    with pytest.raises(aa.ChecksumMismatchError):
        await aa.publish_archive(storage, TABLE, io.BytesIO(body), dump=dump, manifest={})

    assert aa.manifest_key(TABLE) not in storage.objects


async def test_publish_writes_data_then_manifest() -> None:
    body, sha, size = _gzip(b"id\n1\n")
    storage = FakeStorage()
    dump = aa.DumpResult(rows=1, sha256=sha, size_bytes=size)

    await aa.publish_archive(storage, TABLE, io.BytesIO(body), dump=dump, manifest={"rows": 1})

    assert storage.saved == [aa.data_key(TABLE), aa.manifest_key(TABLE)]
    assert storage.objects[aa.data_key(TABLE)] == body


async def test_verify_missing_object() -> None:
    with pytest.raises(aa.ChecksumMismatchError):
        await aa.verify_object(FakeStorage(), "audit/x/data.csv.gz", sha256="a" * 64, size_bytes=1)


class _FakeCursor:
    def __init__(self, row: tuple[Any, ...] | None, rows: list[tuple[Any, ...]]) -> None:
        self._row, self._rows = row, rows

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Tx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    """Scripted ``AsyncConnection`` for the idempotent path (manifest already present)."""

    autocommit = True

    def __init__(self, rows_in_table: int) -> None:
        self.rows_in_table = rows_in_table
        self.statements: list[str] = []

    def transaction(self, **_: Any) -> _Tx:
        return _Tx()

    async def execute(self, query: Any, params: Any = None) -> _FakeCursor:
        text = query if isinstance(query, str) else query.as_string(None)
        self.statements.append(text)
        if "FROM pg_class c JOIN pg_namespace" in text:
            return _FakeCursor(None, [(TABLE,)])
        if "pg_inherits i" in text:
            return _FakeCursor(None, [])
        if "partman.part_config') IS NOT NULL" in text:
            return _FakeCursor((False,), [])
        if "pg_try_advisory_xact_lock" in text:
            return _FakeCursor((True,), [])
        if "FROM pg_inherits WHERE inhrelid" in text:
            return _FakeCursor((False,), [])
        if text.startswith("SELECT count(*)"):
            return _FakeCursor((self.rows_in_table, None, None), [])
        return _FakeCursor(None, [])


def _seed_manifest(storage: FakeStorage, rows: int, data: bytes, sha: str | None = None) -> None:
    storage.objects[aa.data_key(TABLE)] = data
    storage.objects[aa.manifest_key(TABLE)] = json.dumps(
        {
            "table": TABLE,
            "schema": "archive",
            "format": "csv+gzip",
            "rows": rows,
            "sha256": sha or hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }
    ).encode()


async def test_idempotent_path_drops_without_reupload() -> None:
    storage = FakeStorage()
    _seed_manifest(storage, rows=3, data=b"gzdata")
    conn = _FakeConn(rows_in_table=3)

    report = await aa.archive_partitions(conn, storage)  # type: ignore[arg-type]

    assert [item.reused for item in report.archived] == [True]
    assert storage.saved == []
    assert any(stmt.startswith("DROP TABLE") for stmt in conn.statements)
    assert report.exit_code == aa.EXIT_OK


async def test_tampered_object_is_not_dropped() -> None:
    storage = FakeStorage()
    _seed_manifest(storage, rows=3, data=b"gzdata", sha="b" * 64)
    conn = _FakeConn(rows_in_table=3)

    report = await aa.archive_partitions(conn, storage)  # type: ignore[arg-type]

    assert [f.error_type for f in report.failed] == ["ChecksumMismatchError"]
    assert not any(stmt.startswith("DROP TABLE") for stmt in conn.statements)
    assert report.exit_code == aa.EXIT_INTEGRITY


async def test_manifest_row_mismatch_is_not_dropped() -> None:
    storage = FakeStorage()
    _seed_manifest(storage, rows=5, data=b"gzdata")
    conn = _FakeConn(rows_in_table=3)

    report = await aa.archive_partitions(conn, storage)  # type: ignore[arg-type]

    assert [f.error_type for f in report.failed] == ["ManifestMismatchError"]
    assert not any(stmt.startswith("DROP TABLE") for stmt in conn.statements)


async def test_requires_autocommit() -> None:
    conn = _FakeConn(rows_in_table=0)
    conn.autocommit = False
    with pytest.raises(aa.AuditArchiveConfigError):
        await aa.archive_partitions(conn, FakeStorage())  # type: ignore[arg-type]


def test_exit_codes() -> None:
    assert aa.exit_code_for(aa.ChecksumMismatchError("x")) == aa.EXIT_INTEGRITY
    assert aa.exit_code_for(aa.ArchiveTooLargeError("x")) == aa.EXIT_INTEGRITY
    assert aa.exit_code_for(aa.PartitionCollisionError("x")) == aa.EXIT_INTEGRITY
    assert aa.exit_code_for(StorageUnavailableError("x")) == aa.EXIT_ERROR
    assert aa.exit_code_for(OSError("x")) == aa.EXIT_ERROR
    try:
        Settings(_env_file=None, postgres={"port": "bad"})
    except pydantic.ValidationError as error:
        assert aa.exit_code_for(error) == aa.EXIT_INTEGRITY
    assert aa.ArchiveReport(dry_run=False).exit_code == aa.EXIT_OK


def test_cli_rejects_bad_limit() -> None:
    with pytest.raises(SystemExit):
        aa.main(["--limit", "0"])
