"""Archive detached audit partitions (``archive.audit_logs_pYYYYMMDD``) and drop them.

pg_partman (``retention_schema = 'archive'``) moves expired ``public.audit_logs`` partitions
into the ``archive`` schema, where no retention applies any more. This module exports every
such table to the audit archive storage (:data:`AuditArchiveStorage`, keys under ``audit/``)
and drops it only after the upload has been verified.

Per partition, in its own transaction (``SET LOCAL lock_timeout = '5s'``):

1. ``pg_try_advisory_xact_lock`` on the table name - a concurrent archiver skips the table
   instead of uploading it twice; then ``LOCK TABLE ... ACCESS EXCLUSIVE`` - nothing can read
   or change the table between COPY and DROP;
2. the table must not be attached to a parent (``pg_inherits``);
3. ``audit/<t>/manifest.json`` already present -> idempotent path: manifest rows must equal
   the table rows and the data object must match the manifest sha256/size, then DROP
   (resume after a crash between upload and DROP); any mismatch -> exit 2, nothing changed;
4. ``COPY ... TO STDOUT (FORMAT csv, HEADER)`` -> deterministic gzip (``mtime=0``) in a temp
   file, sha256/size over the compressed bytes, rows from the COPY command tag;
5. upload ``data.csv.gz``, verify (``exists`` + re-download sha256/size), upload manifest;
6. ``DROP TABLE`` -> COMMIT.

Name collisions (design-plan, "Уникальность имени партиции"): an ``archive`` table with the
same name as a partition still attached to ``public.audit_logs`` makes the next pg_partman
detach fail with ``relation already exists``. Such tables are reported (exit 2) and left for
the operator: the state results from a manual rollback/rename and is ambiguous.

Objects larger than 5 GiB (single ``PutObject`` limit) raise :class:`ArchiveTooLargeError`;
multipart upload is a future task.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import traceback
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, BinaryIO, Final, cast

import psycopg
import pydantic
from dishka import make_async_container
from psycopg import sql
from psycopg.abc import Params

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.shared_kernel.domain.storage import StorageObjectNotFoundError, StorageService
from app.shared_kernel.infrastructure.providers import AuditArchiveStorage, StorageProvider

__all__ = [
    "ARCHIVE_SCHEMA",
    "CANDIDATE_RE",
    "EXIT_CONFIG",
    "EXIT_ERROR",
    "EXIT_INTEGRITY",
    "EXIT_OK",
    "KEY_PREFIX",
    "MAX_SINGLE_PUT_BYTES",
    "ArchiveIntegrityError",
    "ArchiveReport",
    "ArchiveTooLargeError",
    "ArchivedPartition",
    "AttachedPartitionError",
    "AuditArchiveConfigError",
    "AuditArchiveError",
    "ChecksumMismatchError",
    "DumpResult",
    "GzipDigestWriter",
    "Manifest",
    "ManifestMismatchError",
    "PartitionCollisionError",
    "PartitionFailure",
    "PlannedPartition",
    "archive_partitions",
    "build_manifest",
    "connect",
    "data_key",
    "exit_code_for",
    "find_collisions",
    "is_candidate",
    "main",
    "manifest_key",
    "parse_manifest",
    "publish_archive",
    "run_archive",
    "verify_object",
]

EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_INTEGRITY: Final = 2
#: Misconfiguration the operator must fix (e.g. ``--tmp-dir`` not writable). Shares the
#: value with :data:`EXIT_INTEGRITY`: both mean "do not retry, a human has to act".
EXIT_CONFIG: Final = 2

ARCHIVE_SCHEMA: Final = "archive"
PARENT_TABLE: Final = "public.audit_logs"
KEY_PREFIX: Final = "audit/"
CANDIDATE_RE: Final = re.compile(r"^audit_logs_p\d{8}$")
#: S3 single ``PutObject`` limit (5 GiB).
MAX_SINGLE_PUT_BYTES: Final = 5 * 1024**3
TOOL_VERSION: Final = "1"
ARCHIVE_FORMAT: Final = "csv+gzip"
DATA_CONTENT_TYPE: Final = "application/gzip"
MANIFEST_CONTENT_TYPE: Final = "application/json"
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")

_LIST_ARCHIVE_TABLES: Final = (
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'archive' AND c.relkind IN ('r', 'p') ORDER BY c.relname"
)
_LIVE_CHILDREN: Final = (
    "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
    "WHERE i.inhparent = to_regclass('public.audit_logs') ORDER BY c.relname"
)
_HAS_PART_CONFIG: Final = "SELECT to_regclass('partman.part_config') IS NOT NULL"
_RETENTION_SCHEMA: Final = (
    "SELECT retention_schema FROM partman.part_config WHERE parent_table = 'public.audit_logs'"
)
_ADVISORY_LOCK: Final = "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))"
_IS_ATTACHED: Final = "SELECT EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = %s::regclass)"
_RELATION_SIZE: Final = "SELECT pg_total_relation_size(%s::regclass)"

logger = get_logger(__name__)


class AuditArchiveError(Exception):
    """Integrity/configuration problem: the partition is kept (exit code 2)."""


class AuditArchiveConfigError(AuditArchiveError):
    """Invalid configuration or connection mode."""


class ArchiveIntegrityError(AuditArchiveError):
    """Archived data does not match the table or the manifest."""


class ChecksumMismatchError(ArchiveIntegrityError):
    """The stored data object differs from the expected sha256/size (or is missing)."""


class ManifestMismatchError(ArchiveIntegrityError):
    """The manifest is invalid or does not describe the current table."""


class ArchiveTooLargeError(AuditArchiveError):
    """The compressed archive exceeds the single ``PutObject`` limit (multipart: future)."""


class PartitionCollisionError(AuditArchiveError):
    """An ``archive`` table has the name of a partition still attached to the parent."""


class AttachedPartitionError(AuditArchiveError):
    """The ``archive`` table is attached to a partitioned parent."""


@dataclass(frozen=True, slots=True)
class DumpResult:
    """Outcome of the COPY export: rows and the sha256/size of the compressed bytes."""

    rows: int
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class Manifest:
    """Validated fields of ``manifest.json`` used for verification."""

    table: str
    rows: int
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ArchivedPartition:
    """A partition archived and dropped; ``reused`` - uploaded by an earlier run."""

    table: str
    rows: int
    size_bytes: int
    sha256: str
    key: str
    reused: bool


@dataclass(frozen=True, slots=True)
class PlannedPartition:
    """Dry-run entry: rows and on-disk size (an upper bound of the archive size)."""

    table: str
    rows: int
    relation_bytes: int


@dataclass(frozen=True, slots=True)
class PartitionFailure:
    """A partition kept because of an integrity/configuration problem."""

    table: str
    error_type: str
    message: str


@dataclass(slots=True)
class ArchiveReport:
    """Outcome of :func:`archive_partitions`."""

    dry_run: bool
    planned: list[PlannedPartition] = field(default_factory=list)
    archived: list[ArchivedPartition] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[PartitionFailure] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_INTEGRITY if self.failed else EXIT_OK


def is_candidate(name: str) -> bool:
    """``True`` for pg_partman 5.x monthly audit partition names (``audit_logs_pYYYYMMDD``)."""
    return CANDIDATE_RE.fullmatch(name) is not None


def data_key(table: str) -> str:
    return f"{KEY_PREFIX}{table}/data.csv.gz"


def manifest_key(table: str) -> str:
    return f"{KEY_PREFIX}{table}/manifest.json"


def find_collisions(archive_tables: Iterable[str], live_children: Iterable[str]) -> list[str]:
    """Names present both in ``archive`` and among attached ``public.audit_logs`` children."""
    return sorted(set(archive_tables) & set(live_children))


class _HashingWriter:
    """File-like sink: sha256 + size of everything written, with a hard size limit."""

    def __init__(self, out: BinaryIO, max_size: int) -> None:
        self._out = out
        self._max_size = max_size
        self._sha = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes, /) -> int:
        self.size += len(data)
        if self.size > self._max_size:
            raise ArchiveTooLargeError(
                f"compressed archive exceeds {self._max_size} bytes (single PutObject limit); "
                "multipart upload is not implemented"
            )
        self._out.write(data)
        self._sha.update(data)
        return len(data)

    def flush(self) -> None:
        self._out.flush()

    @property
    def hexdigest(self) -> str:
        return self._sha.hexdigest()


class GzipDigestWriter:
    """Deterministic gzip (``mtime=0``, no file name) into ``out`` with sha256 of the output."""

    def __init__(self, out: BinaryIO, *, max_size: int = MAX_SINGLE_PUT_BYTES) -> None:
        self._sink = _HashingWriter(out, max_size)
        self._gzip = gzip.GzipFile(
            filename="", mode="wb", fileobj=cast(BinaryIO, self._sink), mtime=0
        )

    def write(self, data: bytes | bytearray | memoryview) -> None:
        self._gzip.write(data)

    def finish(self) -> tuple[str, int]:
        """Flush the gzip trailer; return ``(sha256, size)`` of the compressed bytes."""
        self._gzip.close()
        return self._sink.hexdigest, self._sink.size


def build_manifest(
    *,
    table: str,
    dump: DumpResult,
    columns: Sequence[str],
    min_created_at: datetime | None,
    max_created_at: datetime | None,
    archived_at: datetime,
) -> dict[str, Any]:
    """``manifest.json`` content describing one archived partition."""
    return {
        "table": table,
        "schema": ARCHIVE_SCHEMA,
        "parent": PARENT_TABLE,
        "rows": dump.rows,
        "sha256": dump.sha256,
        "size_bytes": dump.size_bytes,
        "format": ARCHIVE_FORMAT,
        "columns": list(columns),
        "min_created_at": min_created_at.isoformat() if min_created_at else None,
        "max_created_at": max_created_at.isoformat() if max_created_at else None,
        "archived_at": archived_at.isoformat(),
        "tool_version": TOOL_VERSION,
    }


def parse_manifest(raw: bytes, *, table: str) -> Manifest:
    """Validate ``manifest.json`` of ``table``; :class:`ManifestMismatchError` otherwise."""
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestMismatchError(f"{manifest_key(table)} is not valid JSON") from error
    if not isinstance(data, dict):
        raise ManifestMismatchError(f"{manifest_key(table)} is not a JSON object")
    rows, sha, size = data.get("rows"), data.get("sha256"), data.get("size_bytes")
    valid = (
        data.get("table") == table
        and data.get("schema") == ARCHIVE_SCHEMA
        and data.get("format") == ARCHIVE_FORMAT
        and isinstance(rows, int)
        and not isinstance(rows, bool)
        and rows >= 0
        and isinstance(sha, str)
        and _SHA256_RE.fullmatch(sha) is not None
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size > 0
    )
    if not valid:
        raise ManifestMismatchError(f"{manifest_key(table)} does not describe archive.{table}")
    return Manifest(
        table=table, rows=cast(int, rows), sha256=cast(str, sha), size_bytes=cast(int, size)
    )


async def verify_object(storage: StorageService, key: str, *, sha256: str, size_bytes: int) -> None:
    """Re-download ``key`` and compare sha256/size; :class:`ChecksumMismatchError` otherwise."""
    digest = hashlib.sha256()
    size = 0
    try:
        async for chunk in storage.get_stream(key):
            digest.update(chunk)
            size += len(chunk)
    except StorageObjectNotFoundError as error:
        raise ChecksumMismatchError(f"{key} is missing in the archive storage") from error
    if size != size_bytes or digest.hexdigest() != sha256:
        raise ChecksumMismatchError(
            f"{key} does not match: expected sha256={sha256} size={size_bytes}, "
            f"got sha256={digest.hexdigest()} size={size}"
        )


async def publish_archive(
    storage: StorageService,
    table: str,
    payload: BinaryIO,
    *,
    dump: DumpResult,
    manifest: Mapping[str, Any],
) -> None:
    """Upload ``data.csv.gz``, verify it, then upload ``manifest.json`` (last, as a commit)."""
    key = data_key(table)
    payload.seek(0)
    await storage.save(
        key,
        payload,
        content_type=DATA_CONTENT_TYPE,
        metadata={"sha256": dump.sha256, "rows": str(dump.rows)},
    )
    if not await storage.exists(key):
        raise ChecksumMismatchError(f"{key} is missing right after upload")
    await verify_object(storage, key, sha256=dump.sha256, size_bytes=dump.size_bytes)
    body = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode()
    await storage.save(manifest_key(table), io.BytesIO(body), content_type=MANIFEST_CONTENT_TYPE)


_Query = str | sql.SQL | sql.Composed


async def _fetchone(
    conn: psycopg.AsyncConnection[Any], query: _Query, params: Params | None = None
) -> tuple[Any, ...]:
    cursor = await conn.execute(query, params)
    row = await cursor.fetchone()
    if row is None:  # pragma: no cover - the queries always return a row
        raise RuntimeError("query returned no row")
    return tuple(row)


async def _fetch_names(conn: psycopg.AsyncConnection[Any], query: _Query) -> list[str]:
    cursor = await conn.execute(query)
    return [str(row[0]) for row in await cursor.fetchall()]


def _ident(table: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(ARCHIVE_SCHEMA), sql.Identifier(table))


async def _load_manifest(storage: StorageService, table: str) -> Manifest | None:
    try:
        raw = await storage.get(manifest_key(table))
    except StorageObjectNotFoundError:
        return None
    return parse_manifest(raw, table=table)


async def _table_stats(
    conn: psycopg.AsyncConnection[Any], table: str
) -> tuple[int, datetime | None, datetime | None]:
    row = await _fetchone(
        conn,
        sql.SQL("SELECT count(*), min(created_at), max(created_at) FROM {}").format(_ident(table)),
    )
    return int(row[0]), row[1], row[2]


async def _columns(conn: psycopg.AsyncConnection[Any], table: str) -> list[str]:
    cursor = await conn.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(_ident(table)))
    return [column.name for column in cursor.description or ()]


async def _dump_table(
    conn: psycopg.AsyncConnection[Any], table: str, out: BinaryIO, *, max_size: int
) -> DumpResult:
    """``COPY ... TO STDOUT`` into ``out`` through :class:`GzipDigestWriter`."""
    copy_sql = sql.SQL(
        "COPY (SELECT * FROM {} ORDER BY created_at, id) TO STDOUT WITH (FORMAT csv, HEADER true)"
    ).format(_ident(table))
    writer = GzipDigestWriter(out, max_size=max_size)
    async with conn.cursor() as cursor:
        async with cursor.copy(copy_sql) as copy:
            async for chunk in copy:
                writer.write(chunk)
        rows = cursor.rowcount
    sha, size = writer.finish()
    return DumpResult(rows=rows, sha256=sha, size_bytes=size)


async def _drop_table(conn: psycopg.AsyncConnection[Any], table: str) -> None:
    await conn.execute(sql.SQL("DROP TABLE {}").format(_ident(table)))


async def _archive_one(
    conn: psycopg.AsyncConnection[Any],
    storage: StorageService,
    table: str,
    *,
    tmp_dir: str | None,
    max_size: int,
) -> ArchivedPartition | None:
    """Archive and drop one table in its own transaction; ``None`` - locked by another run."""
    async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '5s'")
        (locked,) = await _fetchone(conn, _ADVISORY_LOCK, (f"audit_archive:archive.{table}",))
        if not locked:
            logger.warning("audit_archive_partition_busy", table=table)
            return None
        (attached,) = await _fetchone(conn, _IS_ATTACHED, (f"{ARCHIVE_SCHEMA}.{table}",))
        if attached:
            raise AttachedPartitionError(f"archive.{table} is attached to a partitioned parent")
        await conn.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(_ident(table)))
        rows, min_created, max_created = await _table_stats(conn, table)
        key = data_key(table)

        manifest = await _load_manifest(storage, table)
        if manifest is not None:
            if manifest.rows != rows:
                raise ManifestMismatchError(
                    f"{manifest_key(table)} has rows={manifest.rows}, archive.{table} has {rows}"
                )
            await verify_object(
                storage, key, sha256=manifest.sha256, size_bytes=manifest.size_bytes
            )
            await _drop_table(conn, table)
            result = ArchivedPartition(
                table, rows, manifest.size_bytes, manifest.sha256, key, reused=True
            )
        else:
            columns = await _columns(conn, table)
            with tempfile.TemporaryFile(dir=tmp_dir, prefix=f"{table}.", suffix=".csv.gz") as tmp:
                payload = cast(BinaryIO, tmp)
                dump = await _dump_table(conn, table, payload, max_size=max_size)
                if dump.rows >= 0 and dump.rows != rows:
                    raise ArchiveIntegrityError(
                        f"COPY exported {dump.rows} rows, archive.{table} has {rows}"
                    )
                dump = DumpResult(rows=rows, sha256=dump.sha256, size_bytes=dump.size_bytes)
                document = build_manifest(
                    table=table,
                    dump=dump,
                    columns=columns,
                    min_created_at=min_created,
                    max_created_at=max_created,
                    archived_at=datetime.now(UTC),
                )
                await publish_archive(storage, table, payload, dump=dump, manifest=document)
            await _drop_table(conn, table)
            result = ArchivedPartition(table, rows, dump.size_bytes, dump.sha256, key, reused=False)
    logger.info(
        "audit_partition_archived",
        table=table,
        rows=result.rows,
        size_bytes=result.size_bytes,
        sha256=result.sha256,
        key=result.key,
        reused=result.reused,
    )
    return result


async def _plan(
    conn: psycopg.AsyncConnection[Any], tables: Sequence[str]
) -> list[PlannedPartition]:
    planned: list[PlannedPartition] = []
    async with conn.transaction(force_rollback=True):
        for table in tables:
            rows, _, _ = await _table_stats(conn, table)
            (size,) = await _fetchone(conn, _RELATION_SIZE, (f"{ARCHIVE_SCHEMA}.{table}",))
            planned.append(PlannedPartition(table, rows, int(size)))
            logger.info("audit_archive_planned", table=table, rows=rows, relation_bytes=size)
    return planned


async def _check_retention_schema(conn: psycopg.AsyncConnection[Any]) -> None:
    (has_config,) = await _fetchone(conn, _HAS_PART_CONFIG)
    if not has_config:
        logger.warning("audit_archive_part_config_missing")
        return
    cursor = await conn.execute(_RETENTION_SCHEMA)
    row = await cursor.fetchone()
    schema = row[0] if row else None
    if schema != ARCHIVE_SCHEMA:
        logger.warning("audit_archive_retention_schema_unexpected", retention_schema=schema)


async def archive_partitions(
    conn: psycopg.AsyncConnection[Any],
    storage: StorageService,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    tmp_dir: str | None = None,
    max_size_bytes: int = MAX_SINGLE_PUT_BYTES,
) -> ArchiveReport:
    """Archive candidate tables of the ``archive`` schema (ascending by name).

    ``conn`` must be in autocommit mode: every partition gets its own transaction.
    Integrity/configuration problems of a partition are recorded in
    :attr:`ArchiveReport.failed` (the partition is kept, the run continues); storage and
    database errors propagate (the current partition is rolled back).
    """
    if not conn.autocommit:
        raise AuditArchiveConfigError("connection must be in autocommit mode")
    report = ArchiveReport(dry_run=dry_run)
    tables = await _fetch_names(conn, _LIST_ARCHIVE_TABLES)
    await _check_retention_schema(conn)
    collisions = find_collisions(tables, await _fetch_names(conn, _LIVE_CHILDREN))
    for name in collisions:
        message = (
            f"archive.{name} has the name of a partition still attached to {PARENT_TABLE}: "
            "the next pg_partman detach will fail with 'relation already exists'; "
            "resolve manually"
        )
        report.failed.append(PartitionFailure(name, PartitionCollisionError.__name__, message))
        logger.error("audit_archive_name_collision", table=name, error=message)
    candidates: list[str] = []
    for name in tables:
        if not is_candidate(name):
            report.ignored.append(name)
            logger.warning("audit_archive_table_ignored", table=name)
        elif name not in collisions:
            candidates.append(name)
    if limit is not None:
        candidates = candidates[:limit]
    if dry_run:
        report.planned = await _plan(conn, candidates)
        return report
    for name in candidates:
        try:
            result = await _archive_one(
                conn, storage, name, tmp_dir=tmp_dir, max_size=max_size_bytes
            )
        except AuditArchiveError as error:
            report.failed.append(PartitionFailure(name, type(error).__name__, str(error)))
            logger.error(
                "audit_archive_partition_failed",
                table=name,
                error_type=type(error).__name__,
                error=str(error),
            )
            continue
        if result is None:
            report.skipped.append(name)
        else:
            report.archived.append(result)
    return report


async def connect(settings: Settings) -> psycopg.AsyncConnection[Any]:
    """Autocommit async connection from ``POSTGRES_*`` settings."""
    pg = settings.postgres
    if pg.password is None:
        raise AuditArchiveConfigError("POSTGRES_PASSWORD is not configured")
    return await psycopg.AsyncConnection.connect(
        host=pg.host,
        port=pg.port,
        dbname=pg.db,
        user=pg.user,
        password=pg.password.get_secret_value(),
        connect_timeout=10,
        autocommit=True,
        application_name="archive_audit_partitions",
    )


def exit_code_for(error: BaseException) -> int:
    """``2`` for integrity/configuration problems, ``1`` for anything else (transient)."""
    if isinstance(error, AuditArchiveError | pydantic.ValidationError):
        return EXIT_INTEGRITY
    return EXIT_ERROR


def _first_line(error: BaseException) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else ""


def _log_failure(error: BaseException) -> int:
    code = exit_code_for(error)
    details: dict[str, Any] = {}
    if code == EXIT_ERROR and not isinstance(error, psycopg.OperationalError):
        # Not ``exc_info=True``: ``dict_tracebacks`` renders frame locals (connection password).
        details["traceback"] = "".join(traceback.format_exception(error))
    logger.error(
        "audit_archive_failed",
        error_type=type(error).__name__,
        error=_first_line(error),
        exit_code=code,
        **details,
    )
    return code


async def run_archive(
    settings: Settings,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    tmp_dir: str | None = None,
) -> int:
    """Build the archive storage (dishka) and a PG connection, archive, return the exit code."""
    try:
        container = make_async_container(StorageProvider(), context={Settings: settings})
        try:
            storage = await container.get(AuditArchiveStorage)
            async with await connect(settings) as conn:
                report = await archive_partitions(
                    conn, storage, dry_run=dry_run, limit=limit, tmp_dir=tmp_dir
                )
        finally:
            await container.close()
    except Exception as error:
        return _log_failure(error)
    logger.info(
        "audit_archive_done",
        dry_run=report.dry_run,
        planned=[item.table for item in report.planned],
        archived=[item.table for item in report.archived],
        skipped=report.skipped,
        failed=[item.table for item in report.failed],
        ignored=report.ignored,
        exit_code=report.exit_code,
    )
    return report.exit_code


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def check_tmp_dir(path: str) -> str | None:
    """Make sure ``path`` exists and the current uid/gid can create files in it.

    No fallback to the system tmp (decision A): spool files of the archive must stay on the
    logs volume, and a bind mount owned by another uid is an operator error that has to be
    fixed on the host, not hidden.

    Returns:
        ``None`` when the directory is usable, otherwise a message for the operator with the
        uid/gid of this process and the ``chown`` hint.
    """
    try:
        os.makedirs(path, exist_ok=True)
        if not os.access(path, os.W_OK | os.X_OK):
            raise PermissionError(f"no write/search permission on {path}")
        # ``os.access`` ignores ACLs and read-only mounts; a real file settles it.
        with tempfile.TemporaryFile(dir=path):
            pass
    except OSError as error:
        uid, gid = os.getuid(), os.getgid()
        return (
            f"--tmp-dir {path} is not writable for uid={uid} gid={gid} ({error}). "
            f"Fix ownership on the host: chown -R {uid}:{gid} <LOGS_DATA_DIR> "
            "(APP_UID:APP_GID from .env), then run `make init-dirs`."
        )
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: ``python ops/scripts/archive_audit_partitions.py [--dry-run] [--limit N]``."""
    parser = argparse.ArgumentParser(
        prog="archive_audit_partitions",
        description="Archive archive.audit_logs_pYYYYMMDD partitions to object storage, drop.",
    )
    parser.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    parser.add_argument("--limit", type=_positive_int, default=None, help="max partitions per run")
    parser.add_argument(
        "--tmp-dir", default=None, help="directory for temporary archives (default: system tmp)"
    )
    args = parser.parse_args(argv)
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    tmp_dir: str | None = args.tmp_dir
    if tmp_dir:
        problem = check_tmp_dir(tmp_dir)
        if problem is not None:
            logger.error(
                "audit_archive_tmp_dir_not_writable",
                tmp_dir=tmp_dir,
                detail=problem,
                exit_code=EXIT_CONFIG,
            )
            print(problem, file=sys.stderr)
            return EXIT_CONFIG
    try:
        settings = get_settings()
    except Exception as error:
        return _log_failure(error)
    return asyncio.run(
        run_archive(settings, dry_run=args.dry_run, limit=args.limit, tmp_dir=tmp_dir)
    )
