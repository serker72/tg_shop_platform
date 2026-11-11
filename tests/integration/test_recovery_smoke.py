"""Recovery smoke test (TZ section 34, ``docs/ops/recovery.md``).

Simulates the "clean server" part of the recovery procedure on the test stack:

1. an empty scratch ``<POSTGRES_DB>_mig_<hex8>_test`` database is brought to ``head`` by
   ``alembic upgrade head`` and the platform setup CLI (the same steps as ``migrate.sh``);
2. the schema/seed a restored platform relies on is checked (revision, ``system_settings``
   singleton, languages + RBAC seed, pg_partman ``part_config`` of both log tables);
3. an object is written to MinIO through ``StorageService``, the local copy is "lost" and
   the object is read back and compared by sha256.

Everything created is removed in ``finally`` (scratch DB dropped with FORCE by the
``scratch_db`` fixture, the object deleted). Without infrastructure the test skips, with
``REQUIRE_INFRA=1`` (``make test-integration``) it fails.
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.config import Settings
from app.modules.iam.models import SEED_LANGUAGES
from app.modules.iam.permissions import SEED_PERMISSIONS, SEED_ROLE_PERMISSIONS, SEED_ROLES
from app.modules.system.models import SINGLETON_ID
from app.shared_kernel.domain.storage import StorageService, StorageUnavailableError
from app.shared_kernel.infrastructure.storage.minio import MinIOStorage, build_minio_client
from tests.integration._cli import assert_no_password, cli_env, run_cli
from tests.integration.conftest import ALEMBIC_INI, Query, infra_unavailable, run_alembic

pytestmark = pytest.mark.integration

PREFIX = "test/recovery"
LOG_TABLES = [("public.audit_logs",), ("public.user_activity",)]


def _head_revision() -> str:
    head = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
    assert head is not None
    return head


def _restore_database(settings: Settings, url: str, query: Query) -> None:
    """Empty DB -> ``alembic upgrade head`` -> platform setup; verify the restored state."""
    assert query("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") == [(0,)]

    run_alembic(url, command.upgrade, "head")
    setup = run_cli([sys.executable, "-m", "app.entrypoints.cli.setup"], cli_env(settings, url))
    output = setup.stdout + setup.stderr
    assert setup.returncode == 0, output
    assert "platform_setup_done" in setup.stdout
    assert settings.postgres.password is not None
    assert_no_password(output, settings.postgres.password.get_secret_value())

    assert query("SELECT version_num FROM alembic_version") == [(_head_revision(),)]
    assert query("SELECT id FROM system_settings") == [(SINGLETON_ID,)]

    languages = {row[0] for row in query("SELECT code FROM languages")}
    assert {code for code, _, _ in SEED_LANGUAGES} <= languages
    assert query("SELECT count(*) FROM languages WHERE is_default") == [(1,)]

    permissions = {row[0] for row in query("SELECT slug FROM permissions")}
    assert {slug for slug, _ in SEED_PERMISSIONS} <= permissions
    roles = {row[0] for row in query("SELECT slug FROM roles WHERE is_system")}
    assert {slug for slug, _ in SEED_ROLES} <= roles
    links = {
        (str(role), str(permission))
        for role, permission in query(
            "SELECT r.slug, p.slug FROM role_permissions rp "
            "JOIN roles r ON r.id = rp.role_id JOIN permissions p ON p.id = rp.permission_id"
        )
    }
    expected_links = {
        (role, permission) for role, slugs in SEED_ROLE_PERMISSIONS.items() for permission in slugs
    }
    assert expected_links <= links

    assert query("SELECT parent_table FROM partman.part_config ORDER BY parent_table") == LOG_TABLES


def _storage(settings: Settings) -> StorageService:
    if settings.minio.secret_key is None:
        infra_unavailable("MINIO_SECRET_KEY is not configured: cannot authenticate to MinIO")
    return MinIOStorage(client=build_minio_client(settings), settings=settings)


async def _restore_object(settings: Settings) -> None:
    """Write an object, drop the local copy, read it back from MinIO, compare sha256."""
    storage = _storage(settings)
    key = f"{PREFIX}/{uuid.uuid4().hex}/restore.bin"
    payload = os.urandom(64 * 1024)
    expected = hashlib.sha256(payload).hexdigest()
    try:
        try:
            stored = await storage.save(key, payload)
        except StorageUnavailableError as error:  # pragma: no cover - infrastructure dependent
            if error.code != "storage_bucket_missing":
                infra_unavailable(f"MinIO is not reachable: {error}")
            build_minio_client(settings).create_bucket(
                Bucket=settings.storage.resolved_bucket(settings.minio)
            )
            stored = await storage.save(key, payload)
        assert stored.size == len(payload)
        del payload  # the "local copy" is lost; only the object storage has the data

        assert await storage.exists(key) is True
        restored = await storage.get(key)
        assert hashlib.sha256(restored).hexdigest() == expected
    finally:
        try:
            await storage.delete(key)
        except StorageUnavailableError:  # pragma: no cover - best effort
            pass


async def test_restore_empty_db_and_object(
    settings: Settings, scratch_db: tuple[str, Query]
) -> None:
    url, query = scratch_db
    _restore_database(settings, url, query)
    await _restore_object(settings)
