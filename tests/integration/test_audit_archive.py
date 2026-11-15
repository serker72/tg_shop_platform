"""Integration tests of the audit archiver (scratch PG database + temporary MinIO bucket)."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import make_url

from app.core.config import Settings
from app.modules.logging import audit_archive as aa
from app.shared_kernel.infrastructure.storage.minio import build_minio_client
from tests.integration.conftest import ROOT, Query, infra_unavailable

pytestmark = [pytest.mark.integration, pytest.mark.req("R7")]


@pytest.fixture
def archive_bucket(settings: Settings) -> Iterator[str]:
    """Temporary bucket used as ``MINIO_AUDIT_ARCHIVE_BUCKET`` (decision A), removed after."""
    client = build_minio_client(settings)
    name = f"audit-test-{uuid.uuid4().hex[:12]}"
    try:
        client.create_bucket(Bucket=name)
    except Exception as error:  # pragma: no cover - infrastructure dependent
        infra_unavailable(f"MinIO is not reachable: {type(error).__name__}")
    try:
        yield name
    finally:
        for item in client.list_objects_v2(Bucket=name).get("Contents", []):
            client.delete_object(Bucket=name, Key=item["Key"])
        client.delete_bucket(Bucket=name)
        client.close()


@pytest.fixture
def setup(
    settings: Settings, migrated_scratch_db: tuple[str, Query], archive_bucket: str
) -> tuple[Settings, Query, str]:
    """Settings bound to the scratch DB + archive bucket; one detached audit partition."""
    url, query = migrated_scratch_db
    dbname = make_url(url).database
    assert dbname is not None and dbname.endswith("_test")
    scoped = settings.model_copy(
        update={
            "postgres": settings.postgres.model_copy(update={"db": dbname}),
            "minio": settings.minio.model_copy(update={"audit_archive_bucket": archive_bucket}),
        }
    )
    query(
        "INSERT INTO audit_logs (id, created_at, actor_type, action) SELECT gen_random_uuid(), "
        "date_trunc('month', now()) - interval '3 months' + g * interval '1 minute', "
        "'system', 'x' || g FROM generate_series(1, 5) g"
    )
    query("SELECT partman.drop_partition_time('public.audit_logs', p_retention := '1 month')")
    tables = _archive_tables(query)
    assert tables, "drop_partition_time moved nothing to archive"
    return scoped, query, archive_bucket


def _archive_tables(query: Query) -> list[str]:
    return [
        str(row[0])
        for row in query(
            "SELECT relname FROM pg_class WHERE relnamespace = 'archive'::regnamespace "
            "AND relkind = 'r' ORDER BY 1"
        )
    ]


def _non_empty(query: Query) -> str:
    for table in _archive_tables(query):
        if int(query(f"SELECT count(*) FROM archive.{table}")[0][0]):  # noqa: S608
            return table
    raise AssertionError("no non-empty archive table")


def _run(settings: Settings, **kwargs: Any) -> int:
    import asyncio

    return asyncio.run(aa.run_archive(settings, **kwargs))


def _keys(settings: Settings, bucket: str) -> set[str]:
    client = build_minio_client(settings)
    try:
        return {i["Key"] for i in client.list_objects_v2(Bucket=bucket).get("Contents", [])}
    finally:
        client.close()


def _get(settings: Settings, bucket: str, key: str) -> bytes:
    client = build_minio_client(settings)
    try:
        return bytes(client.get_object(Bucket=bucket, Key=key)["Body"].read())
    finally:
        client.close()


def test_archive_roundtrip_and_rerun_is_noop(setup: tuple[Settings, Query, str]) -> None:
    settings, query, bucket = setup
    table = _non_empty(query)

    assert _run(settings) == aa.EXIT_OK

    assert _archive_tables(query) == []
    data = _get(settings, bucket, aa.data_key(table))
    manifest = json.loads(_get(settings, bucket, aa.manifest_key(table)))
    lines = gzip.decompress(data).decode().splitlines()
    assert manifest["rows"] == len(lines) - 1 == 5
    assert manifest["sha256"] == hashlib.sha256(data).hexdigest()
    assert manifest["size_bytes"] == len(data)
    assert lines[0].split(",") == manifest["columns"]
    keys_before = _keys(settings, bucket)

    assert _run(settings) == aa.EXIT_OK
    assert _keys(settings, bucket) == keys_before


def test_manifest_columns_come_from_archived_table(setup: tuple[Settings, Query, str]) -> None:
    """A partition detached before the ``bots`` revision keeps ``bot_id uuid``; the manifest
    and the CSV header follow the table itself, not the current ``AuditLog`` model."""
    settings, query, bucket = setup
    table = _non_empty(query)
    query(f"ALTER TABLE archive.{table} RENAME COLUMN telegram_bot_id TO bot_id")  # noqa: S608
    query(f"ALTER TABLE archive.{table} ALTER COLUMN bot_id TYPE uuid USING NULL")  # noqa: S608

    assert _run(settings) == aa.EXIT_OK

    manifest = json.loads(_get(settings, bucket, aa.manifest_key(table)))
    header = gzip.decompress(_get(settings, bucket, aa.data_key(table))).decode().splitlines()[0]
    assert "bot_id" in manifest["columns"]
    assert "telegram_bot_id" not in manifest["columns"]
    assert header.split(",") == manifest["columns"]


def test_crash_between_upload_and_drop_resumes(
    setup: tuple[Settings, Query, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, query, bucket = setup
    table = _non_empty(query)

    async def boom(conn: Any, name: str) -> None:
        raise RuntimeError("crash before DROP")

    monkeypatch.setattr(aa, "_drop_table", boom)
    assert _run(settings) == aa.EXIT_ERROR
    assert table in _archive_tables(query)
    uploaded = _get(settings, bucket, aa.data_key(table))
    monkeypatch.undo()

    saved: list[str] = []
    original = aa.publish_archive

    async def spy(*args: Any, **kwargs: Any) -> None:
        saved.append(args[1])
        await original(*args, **kwargs)

    monkeypatch.setattr(aa, "publish_archive", spy)
    assert _run(settings) == aa.EXIT_OK
    assert table not in _archive_tables(query)
    assert table not in saved
    assert _get(settings, bucket, aa.data_key(table)) == uploaded


def test_too_large_mid_copy_keeps_table(setup: tuple[Settings, Query, str], tmp_path: Any) -> None:
    """RX6: the size limit hit during COPY -> ``ArchiveTooLargeError`` (exit 2), no DROP."""
    import asyncio

    from dishka import make_async_container

    from app.shared_kernel.infrastructure.providers import AuditArchiveStorage, StorageProvider

    settings, query, bucket = setup
    table = _non_empty(query)
    before = _archive_tables(query)
    # Only the non-empty partition: an empty one compresses below 100 bytes and is archived.
    assert before[0] == table

    async def run() -> aa.ArchiveReport:
        container = make_async_container(StorageProvider(), context={Settings: settings})
        try:
            storage = await container.get(AuditArchiveStorage)
            async with await aa.connect(settings) as conn:
                return await aa.archive_partitions(
                    conn, storage, tmp_dir=str(tmp_path), limit=1, max_size_bytes=100
                )
        finally:
            await container.close()

    report = asyncio.run(run())

    assert report.exit_code == aa.EXIT_INTEGRITY
    assert [(f.table, f.error_type) for f in report.failed] == [
        (table, aa.ArchiveTooLargeError.__name__)
    ]
    assert report.archived == []
    assert _archive_tables(query) == before
    assert int(query(f"SELECT count(*) FROM archive.{table}")[0][0]) == 5  # noqa: S608
    assert list(tmp_path.iterdir()) == []
    assert _keys(settings, bucket) == set()


def test_storage_unavailable_keeps_table(setup: tuple[Settings, Query, str]) -> None:
    settings, query, _ = setup
    before = _archive_tables(query)
    broken = settings.model_copy(
        update={"minio": settings.minio.model_copy(update={"endpoint": "127.0.0.1:1"})}
    )

    assert _run(broken) == aa.EXIT_ERROR
    assert _archive_tables(query) == before


def test_corrupted_object_with_manifest_keeps_table(setup: tuple[Settings, Query, str]) -> None:
    settings, query, bucket = setup
    table = _non_empty(query)
    client = build_minio_client(settings)
    try:
        client.put_object(Bucket=bucket, Key=aa.data_key(table), Body=b"other bytes")
        client.put_object(
            Bucket=bucket,
            Key=aa.manifest_key(table),
            Body=json.dumps(
                {
                    "table": table,
                    "schema": "archive",
                    "format": "csv+gzip",
                    "rows": 5,
                    "sha256": "a" * 64,
                    "size_bytes": 11,
                }
            ).encode(),
        )
    finally:
        client.close()

    assert _run(settings) == aa.EXIT_INTEGRITY
    assert table in _archive_tables(query)


def test_dry_run_changes_nothing(setup: tuple[Settings, Query, str]) -> None:
    settings, query, bucket = setup
    before = _archive_tables(query)

    assert _run(settings, dry_run=True) == aa.EXIT_OK

    assert _archive_tables(query) == before
    assert _keys(settings, bucket) == set()


def test_cli_dry_run_subprocess(setup: tuple[Settings, Query, str]) -> None:
    settings, query, bucket = setup
    before = _archive_tables(query)
    env = dict(os.environ)
    env.update(
        {
            "POSTGRES_HOST": settings.postgres.host,
            "POSTGRES_PORT": str(settings.postgres.port),
            "POSTGRES_DB": settings.postgres.db,
            "MINIO_AUDIT_ARCHIVE_BUCKET": bucket,
        }
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "ops/scripts/archive_audit_partitions.py"), "--dry-run"],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _archive_tables(query) == before
