"""Unit tests for :mod:`app.core.config`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import Settings, get_settings

_ENV_KEYS = [
    "PROJECT_ENVIRONMENT",
    "PROJECT_NAME",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "SQLALCHEMY_ECHO",
    "SQLALCHEMY_POOL_SIZE",
    "SQLALCHEMY_MAX_OVERFLOW",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_PASSWORD",
    "REDIS_DB",
    "REDIS_SOCKET_TIMEOUT",
    "RABBITMQ_HOST",
    "RABBITMQ_PORT",
    "RABBITMQ_USER",
    "RABBITMQ_PASSWORD",
    "RABBITMQ_VHOST",
    "MINIO_ENDPOINT",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
    "MINIO_BUCKET",
    "MINIO_SECURE",
    "MINIO_AUDIT_ARCHIVE_BUCKET",
    "ENCRYPTION_KEY",
    "ENCRYPTION_KEYS_OLD",
    "STORAGE_BACKEND",
    "STORAGE_ENDPOINT",
    "STORAGE_REGION",
    "STORAGE_BUCKET",
    "LOGS_DIR",
    "LOGS_BUFFER_QUEUE_SIZE",
    "LOGS_BUFFER_BATCH_SIZE",
    "LOGS_FILE_MAX_BYTES",
]

# Unknown env variables from ``.env.example`` that must never break validation.
_FOREIGN_ENV_KEYS = [
    "POSTGRES_PORT_HOST",
    "POSTGRES_DATA_DIR",
    "MINIO_IMAGE",
    "MINIO_DATA_DIR",
    "MINIO_API_PORT_HOST",
    "RABBITMQ_MGMT_PORT_HOST",
    "APP_UID",
    "APP_ENV_FILE",
    "LOGS_DATA_DIR",
]


def make_settings(env_file: str | None = None, **overrides: Any) -> Settings:
    """Build settings with a controlled dotenv source.

    ``_env_file`` is a documented runtime-only ``BaseSettings`` argument missing from
    the pydantic-settings stubs, hence the single targeted ``type: ignore``.
    """
    return Settings(_env_file=env_file, **overrides)


def write_env_file(tmp_path: Any, content: str) -> str:
    """Write a dotenv file and return its path as a string."""
    env_file = tmp_path / "test.env"
    env_file.write_text(content, encoding="utf-8")
    return str(env_file)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove known config env variables so each test starts from defaults."""
    for key in [*_ENV_KEYS, *_FOREIGN_ENV_KEYS]:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_without_env() -> None:
    settings = make_settings(env_file=None)

    assert settings.project_environment == "loc"
    assert settings.project_name == "tg_shop_platform"
    assert settings.postgres.host == "postgres"
    assert settings.postgres.port == 5432
    assert settings.postgres.db == "tg_shop_platform"
    assert settings.sqlalchemy.echo is False
    assert settings.sqlalchemy.pool_size == 5
    assert settings.sqlalchemy.max_overflow == 5
    assert settings.redis.db == 0
    assert settings.redis.host == "redis"
    assert settings.rabbitmq.vhost == "/"
    assert settings.minio.endpoint == "minio:9000"
    assert settings.encryption.key is None
    assert settings.encryption_key_list == []


def test_logs_defaults() -> None:
    logs = make_settings(env_file=None).logs

    assert str(logs.dir) == "/app/logs"
    assert logs.buffer_queue_size == 10_000
    assert logs.buffer_batch_size == 500
    assert logs.buffer_flush_interval_seconds == 1.0
    assert logs.buffer_db_retry_attempts == 3
    assert logs.file_max_open == 256
    assert logs.file_max_bytes == 50 * 1024 * 1024


def test_logs_dir_env_and_host_dir_not_mixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOGS_DATA_DIR", "/srv/host/logs")
    assert str(make_settings(env_file=None).logs.dir) == "/app/logs"

    monkeypatch.setenv("LOGS_DIR", "/custom/logs")
    monkeypatch.setenv("LOGS_BUFFER_QUEUE_SIZE", "7")
    logs = make_settings(env_file=None).logs
    assert str(logs.dir) == "/custom/logs"
    assert logs.buffer_queue_size == 7


def test_logs_dotenv_is_read(tmp_path: Any) -> None:
    env_file = write_env_file(tmp_path, "LOGS_DIR=/from/dotenv\nLOGS_FILE_MAX_BYTES=1024\n")
    logs = make_settings(env_file).logs
    assert str(logs.dir) == "/from/dotenv"
    assert logs.file_max_bytes == 1024


@pytest.mark.parametrize("key", ["LOGS_BUFFER_QUEUE_SIZE", "LOGS_BUFFER_BATCH_SIZE"])
def test_logs_non_positive_rejected(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setenv(key, "0")
    with pytest.raises(ValidationError):
        make_settings(env_file=None)


def test_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_DB", "custom_db")
    monkeypatch.setenv("REDIS_DB", "3")

    settings = make_settings()

    assert settings.postgres.host == "db.internal"
    assert settings.postgres.db == "custom_db"
    assert settings.redis.db == 3


def test_dotenv_file_values_are_read(tmp_path: Any) -> None:
    env_file = write_env_file(
        tmp_path,
        "POSTGRES_HOST=filehost\nMINIO_AUDIT_ARCHIVE_BUCKET=file-bucket\n"
        "MINIO_SECRET_KEY=file-secret\n",
    )

    settings = make_settings(env_file)

    assert settings.postgres.host == "filehost"
    assert settings.minio.audit_archive_bucket == "file-bucket"
    assert settings.minio.secret_key is not None
    assert settings.minio.secret_key.get_secret_value() == "file-secret"


def test_env_takes_precedence_over_dotenv(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = write_env_file(tmp_path, "POSTGRES_HOST=filehost\n")
    monkeypatch.setenv("POSTGRES_HOST", "envhost")

    assert make_settings(env_file).postgres.host == "envhost"


def test_missing_dotenv_file_falls_back_to_defaults(tmp_path: Any) -> None:
    settings = make_settings(str(tmp_path / "missing.env"))

    assert settings.postgres.host == "postgres"
    assert settings.encryption_key_list == []


def test_empty_encryption_key_in_dotenv(tmp_path: Any) -> None:
    env_file = write_env_file(tmp_path, "ENCRYPTION_KEY=\nENCRYPTION_KEYS_OLD=\n")

    settings = make_settings(env_file)

    assert settings.encryption.key is None
    assert settings.encryption.keys_old is None
    assert settings.encryption_key_list == []


def test_encryption_key_from_dotenv(tmp_path: Any) -> None:
    key = Fernet.generate_key().decode()
    env_file = write_env_file(tmp_path, f"ENCRYPTION_KEY={key}\n")

    assert make_settings(env_file).encryption_key_list == [key]


def test_encryption_keys_old_from_dotenv(tmp_path: Any) -> None:
    key = Fernet.generate_key().decode()
    env_file = write_env_file(tmp_path, f'ENCRYPTION_KEY={key}\nENCRYPTION_KEYS_OLD="k1,k2"\n')

    assert make_settings(env_file).encryption_key_list == [key, "k1", "k2"]


def test_postgres_db_from_dotenv(tmp_path: Any) -> None:
    env_file = write_env_file(tmp_path, "POSTGRES_DB=custom_db\n")

    assert make_settings(env_file).postgres.db == "custom_db"


def test_database_url_encodes_special_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_USER", "shop")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss/wo:rd")
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "shop_db")

    url = make_settings().database_url

    assert url.startswith("postgresql+psycopg://")
    parsed = make_url(url)
    assert parsed.password == "p@ss/wo:rd"
    assert parsed.username == "shop"
    assert parsed.host == "postgres"
    assert parsed.port == 5432
    assert parsed.database == "shop_db"


def test_empty_postgres_password_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "")

    settings = make_settings()

    assert settings.postgres.password is None
    with pytest.raises(ValueError, match="POSTGRES_PASSWORD is not configured"):
        _ = settings.database_url


def test_empty_minio_secret_key_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIO_SECRET_KEY", "")

    assert make_settings().minio.secret_key is None


@pytest.mark.parametrize(
    ("secure", "expected"),
    [("false", "http://minio:9000"), ("true", "https://minio:9000")],
)
def test_minio_endpoint_url(monkeypatch: pytest.MonkeyPatch, secure: str, expected: str) -> None:
    monkeypatch.setenv("MINIO_ENDPOINT", "minio:9000")
    monkeypatch.setenv("MINIO_SECURE", secure)

    assert make_settings().minio_endpoint_url == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "minio",
        "minio:",
        "host:",
        ":9000",
        "minio:port",
        "minio:9000:9000",
        "http://minio:9000",
        "minio:0",
        "minio:65536",
    ],
)
def test_minio_endpoint_requires_host_port(monkeypatch: pytest.MonkeyPatch, endpoint: str) -> None:
    monkeypatch.setenv("MINIO_ENDPOINT", endpoint)

    with pytest.raises(ValidationError, match="MINIO_ENDPOINT"):
        make_settings()


@pytest.mark.parametrize("endpoint", ["minio:9000", "127.0.0.1:9000", "[::1]:9000"])
def test_minio_endpoint_accepts_host_port(monkeypatch: pytest.MonkeyPatch, endpoint: str) -> None:
    monkeypatch.setenv("MINIO_ENDPOINT", endpoint)

    assert make_settings().minio.endpoint == endpoint


def _write_cwd_dotenv(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    """Write ``.env`` into a temporary cwd and return the generated Fernet key."""
    key = Fernet.generate_key().decode()
    (tmp_path / ".env").write_text(
        f"ENCRYPTION_KEY={key}\nMINIO_AUDIT_ARCHIVE_BUCKET=file-bucket\nPOSTGRES_DB=file_db\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return key


def test_default_dotenv_reaches_nested_sections(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _write_cwd_dotenv(tmp_path, monkeypatch)

    settings = Settings()

    assert settings.encryption_key_list == [key]
    assert settings.minio.audit_archive_bucket == "file-bucket"
    assert settings.postgres.db == "file_db"


def test_get_settings_reads_default_dotenv(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    key = _write_cwd_dotenv(tmp_path, monkeypatch)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.encryption_key_list == [key]
    assert settings.minio.audit_archive_bucket == "file-bucket"
    assert settings.postgres.db == "file_db"


def test_explicit_none_env_file_skips_default_dotenv(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_cwd_dotenv(tmp_path, monkeypatch)

    settings = make_settings(None)

    assert settings.encryption.key is None
    assert settings.postgres.db == "tg_shop_platform"


def test_section_mapping_keeps_dotenv_for_other_fields(tmp_path: Any) -> None:
    env_file = write_env_file(tmp_path, "POSTGRES_DB=pgdb\nPOSTGRES_HOST=filehost\n")

    settings = make_settings(env_file, postgres={"host": "x"})

    assert settings.postgres.host == "x"
    assert settings.postgres.db == "pgdb"


def test_empty_rabbitmq_vhost_becomes_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RABBITMQ_VHOST", "")

    settings = make_settings()

    assert settings.rabbitmq.vhost == "/"
    assert settings.rabbitmq_url.endswith("/%2F")


def test_project_environment_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROJECT_ENVIRONMENT", "tset")

    with pytest.raises(ValidationError, match="project_environment"):
        make_settings()


def test_project_environment_accepts_known_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROJECT_ENVIRONMENT", "prod")

    assert make_settings().project_environment == "prod"


def test_invalid_port_reports_field_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PORT", "abc")

    with pytest.raises(ValidationError, match="port"):
        make_settings()


def test_foreign_env_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PORT_HOST", "15432")
    monkeypatch.setenv("POSTGRES_DATA_DIR", "/data/pg")
    monkeypatch.setenv("MINIO_IMAGE", "pgsty/minio:latest")
    monkeypatch.setenv("MINIO_DATA_DIR", "/data/minio")
    monkeypatch.setenv("MINIO_API_PORT_HOST", "9000")
    monkeypatch.setenv("RABBITMQ_MGMT_PORT_HOST", "15672")
    monkeypatch.setenv("APP_UID", "1000")
    monkeypatch.setenv("APP_ENV_FILE", ".env")

    settings = make_settings()

    assert settings.rabbitmq.port == 5672
    assert settings.postgres.port == 5432


def test_repr_hides_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "pg-secret-value")
    monkeypatch.setenv("MINIO_SECRET_KEY", "minio-secret-value")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())

    text = repr(make_settings())

    assert "pg-secret-value" not in text
    assert "minio-secret-value" not in text
    assert "encryption_key_list" not in text


def test_get_settings_is_cached() -> None:
    first = get_settings()
    second = get_settings()
    assert first is second

    get_settings.cache_clear()
    third = get_settings()
    assert third is not first


def test_encryption_key_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "current-key")
    monkeypatch.setenv("ENCRYPTION_KEYS_OLD", "old-key-1, old-key-2 ,")

    assert make_settings().encryption_key_list == ["current-key", "old-key-1", "old-key-2"]


def test_rabbitmq_url_encodes_root_vhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RABBITMQ_HOST", "rabbitmq")
    monkeypatch.setenv("RABBITMQ_PORT", "5672")
    monkeypatch.setenv("RABBITMQ_USER", "shop")
    monkeypatch.setenv("RABBITMQ_PASSWORD", "change_me")
    monkeypatch.setenv("RABBITMQ_VHOST", "/")

    assert make_settings().rabbitmq_url == "amqp://shop:change_me@rabbitmq:5672/%2F"


def test_redis_socket_timeout_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make_settings().redis.socket_timeout == 0.3
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT", "1.5")
    assert make_settings().redis.socket_timeout == 1.5


@pytest.mark.parametrize("value", ["0", "-1"])
def test_redis_socket_timeout_must_be_positive(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT", value)
    with pytest.raises(ValidationError):
        make_settings()


def test_redis_url_without_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_HOST", "redis")
    monkeypatch.setenv("REDIS_PASSWORD", "")
    monkeypatch.setenv("REDIS_DB", "1")

    assert make_settings().redis_url == "redis://redis:6379/1"


def test_storage_backend_defaults_to_minio() -> None:
    settings = make_settings(env_file=None)

    assert settings.storage.backend == "minio"
    assert settings.storage.endpoint is None
    assert settings.storage.region is None
    assert settings.storage.bucket is None


def test_storage_backend_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "s3")

    assert make_settings().storage.backend == "s3"


def test_storage_backend_from_dotenv(tmp_path: Any) -> None:
    env_file = write_env_file(
        tmp_path,
        "STORAGE_BACKEND=s3\nSTORAGE_REGION=eu-central-1\n"
        "STORAGE_ENDPOINT=https://s3.example.com\n",
    )

    storage = make_settings(env_file).storage

    assert storage.backend == "s3"
    assert storage.region == "eu-central-1"
    assert storage.endpoint_url == "https://s3.example.com"


def test_storage_backend_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "gcs")

    with pytest.raises(ValidationError, match="backend"):
        make_settings()


def test_storage_bucket_falls_back_to_minio_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIO_BUCKET", "custom-bucket")
    monkeypatch.setenv("STORAGE_BUCKET", "")

    settings = make_settings()

    assert settings.storage.bucket is None
    assert settings.storage.resolved_bucket(settings.minio) == "custom-bucket"


def test_storage_bucket_overrides_minio_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIO_BUCKET", "minio-bucket")
    monkeypatch.setenv("STORAGE_BUCKET", "storage-bucket")

    settings = make_settings()

    assert settings.storage.resolved_bucket(settings.minio) == "storage-bucket"


def test_default_minio_bucket_used_when_storage_bucket_empty() -> None:
    settings = make_settings(env_file=None)

    assert settings.storage.resolved_bucket(settings.minio) == settings.minio.bucket == "tg-shop"


def test_storage_endpoint_without_scheme_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_ENDPOINT", "minio:9000")

    with pytest.raises(ValidationError, match="STORAGE_ENDPOINT must be a full URL"):
        make_settings(env_file=None)


def test_storage_endpoint_without_host_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_ENDPOINT", "https://")

    with pytest.raises(ValidationError, match="STORAGE_ENDPOINT"):
        make_settings(env_file=None)


def test_storage_endpoint_full_url_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_ENDPOINT", "https://s3.example.com")

    settings = make_settings(env_file=None)

    assert settings.storage.endpoint_url == "https://s3.example.com"


def test_empty_storage_credentials_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_ACCESS_KEY", "")
    monkeypatch.setenv("STORAGE_SECRET_KEY", "  ")

    settings = make_settings(env_file=None)

    assert settings.storage.access_key is None
    assert settings.storage.secret_key is None


def test_storage_credentials_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_ACCESS_KEY", "AKIA")
    monkeypatch.setenv("STORAGE_SECRET_KEY", "s3-secret")

    settings = make_settings(env_file=None)

    assert settings.storage.access_key == "AKIA"
    assert settings.storage.secret_key is not None
    assert settings.storage.secret_key.get_secret_value() == "s3-secret"
