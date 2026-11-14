"""Application configuration built on pydantic-settings.

Every settings model (root and nested) reads values from three sources in order of
precedence: explicit init arguments, ``os.environ`` (container path), then the ``.env``
file (local path). Nested models are ``BaseSettings`` subclasses on purpose: a plain
``BaseModel`` never runs the settings sources and silently keeps defaults.

Environment variables that belong to other tools (ports published by docker compose,
bind-mount directories, image names, ...) are not part of these models and are ignored.

Invalid values raise ``ValidationError`` when ``Settings`` is constructed, not on import.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import quote, urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

__all__ = [
    "EncryptionSettings",
    "LogsSettings",
    "MinioSettings",
    "ObservabilitySettings",
    "PostgresSettings",
    "RabbitMQSettings",
    "RedisSettings",
    "SQLAlchemySettings",
    "Settings",
    "StorageSettings",
    "get_settings",
]

# Fallback dotenv file location, relative to the process working directory.
_DEFAULT_ENV_FILE = ".env"

# ``host:port`` or ``[ipv6]:port``; the port range is checked separately.
_ENDPOINT_RE = re.compile(
    r"^(\[[0-9A-Fa-f:.]+\]|[0-9A-Za-z](?:[0-9A-Za-z.\-]*[0-9A-Za-z])?):(\d{1,5})$"
)

# Marks "argument not passed", as opposed to an explicit ``None``.
_UNSET: Final = object()

ProjectEnvironment = Literal["loc", "test", "stage", "prod"]
StorageBackend = Literal["minio", "s3"]


def _base_config(prefix: str, *, env_file: str | None = None) -> SettingsConfigDict:
    """Build the shared config of a settings model.

    ``extra="ignore"`` keeps foreign environment variables from breaking validation:
    with a ``settings_customise_sources`` override pydantic-settings collects *every*
    environment variable, and the dotenv file is exposed as ``ENV_FILE`` with a JSON
    dump of all its keys.
    """
    config = SettingsConfigDict(
        env_prefix=prefix,
        extra="ignore",
        case_sensitive=False,
    )
    if env_file is not None:
        config["env_file"] = env_file
        config["env_file_encoding"] = "utf-8"
    return config


def _normalise_empty_secret(value: object) -> object:
    """Normalise an empty or blank string secret to ``None``."""
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if isinstance(value, str) and not value.strip():
        return None
    return value


class PostgresSettings(BaseSettings):
    """PostgreSQL connection settings (``POSTGRES_*`` env variables)."""

    model_config = _base_config("POSTGRES_")

    host: str = "postgres"
    port: int = 5432
    user: str = "tg_shop_platform"
    # ``None`` means "not configured" and is rejected by ``Settings.database_url``.
    password: SecretStr | None = SecretStr("change_me")
    # Env variable is ``POSTGRES_DB`` while the field is named ``db``.
    db: str = Field(
        default="tg_shop_platform",
        validation_alias=AliasChoices("POSTGRES_DB", "db"),
    )

    @field_validator("password", mode="before")
    @classmethod
    def _empty_password_to_none(cls, value: object) -> object:
        return _normalise_empty_secret(value)


class SQLAlchemySettings(BaseSettings):
    """SQLAlchemy engine tuning (``SQLALCHEMY_*`` env variables)."""

    model_config = _base_config("SQLALCHEMY_")

    echo: bool = False
    pool_size: int = 10
    max_overflow: int = 20


class RedisSettings(BaseSettings):
    """Redis connection settings (``REDIS_*`` env variables)."""

    model_config = _base_config("REDIS_")

    host: str = "redis"
    port: int = 6379
    password: SecretStr = SecretStr("")
    # ``REDIS_DB`` is the database *number*, unrelated to ``POSTGRES_DB``.
    db: int = 0
    #: Connect and read/write timeout, seconds. Redis is only a cache: on a slow or dead
    #: server the callers fall back to PostgreSQL, so the budget is deliberately tiny.
    socket_timeout: float = Field(default=0.3, gt=0, le=5)


class RabbitMQSettings(BaseSettings):
    """RabbitMQ connection settings (``RABBITMQ_*`` env variables)."""

    model_config = _base_config("RABBITMQ_")

    host: str = "rabbitmq"
    port: int = 5672
    user: str = "tg_shop_platform"
    password: SecretStr = SecretStr("change_me")
    vhost: str = "/"

    @field_validator("vhost", mode="before")
    @classmethod
    def _empty_vhost_to_root(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return "/"
        return value


class MinioSettings(BaseSettings):
    """MinIO object storage settings (``MINIO_*`` env variables)."""

    model_config = _base_config("MINIO_")

    # ``host:port`` form without a scheme, e.g. ``minio:9000``.
    endpoint: str = "minio:9000"
    access_key: str = "tg_shop_platform"
    # ``None`` means "not configured"; empty strings are normalised to ``None``.
    secret_key: SecretStr | None = SecretStr("change_me_min_8_chars")
    bucket: str = "tg-shop"
    secure: bool = False
    audit_archive_bucket: str | None = None

    @field_validator("secret_key", mode="before")
    @classmethod
    def _empty_secret_to_none(cls, value: object) -> object:
        return _normalise_empty_secret(value)

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint_format(cls, value: str) -> str:
        match = _ENDPOINT_RE.match(value)
        if match is None:
            raise ValueError(
                "MINIO_ENDPOINT must have the 'host:port' format "
                f"(e.g. 'minio:9000'), got {value!r}"
            )
        if not 0 < int(match.group(2)) < 65536:
            raise ValueError(f"MINIO_ENDPOINT port must be in 1..65535, got {value!r}")
        return value


class EncryptionSettings(BaseSettings):
    """Fernet key material (``ENCRYPTION_*`` env variables)."""

    model_config = _base_config("ENCRYPTION_")

    key: SecretStr | None = None
    # Comma-separated previous keys used during rotation.
    keys_old: SecretStr | None = None

    @field_validator("key", "keys_old", mode="before")
    @classmethod
    def _empty_encryption_to_none(cls, value: object) -> object:
        return _normalise_empty_secret(value)

    @property
    def keys_old_list(self) -> list[str]:
        """Return the parsed list of previous keys."""
        if self.keys_old is None:
            return []
        raw = self.keys_old.get_secret_value()
        return [item.strip() for item in raw.split(",") if item.strip()]


class StorageSettings(BaseSettings):
    """Object storage backend selection (``STORAGE_*`` env variables).

    ``minio`` (default) uses the mandatory MinIO service with its ``MINIO_*`` endpoint and
    credentials; ``MINIO_SECURE`` applies only to this backend. ``s3`` targets AWS S3 or an
    S3-compatible endpoint; ``STORAGE_ENDPOINT``, ``STORAGE_REGION``, ``STORAGE_ACCESS_KEY``
    and ``STORAGE_SECRET_KEY`` apply only to ``STORAGE_BACKEND=s3``. Unset S3 credentials
    (``None``) delegate to the boto3 credential chain (env ``AWS_*``, profile, IAM role).
    The bucket falls back to ``MINIO_BUCKET`` so a deployment switching the backend keeps
    its layout.
    """

    model_config = _base_config("STORAGE_")

    backend: StorageBackend = "minio"
    # Full ``scheme://host:port`` URL for ``s3``; ``None`` means the AWS default endpoint.
    # MinIO always uses ``minio_endpoint_url`` and ignores this field.
    endpoint: str | None = None
    region: str | None = None
    bucket: str | None = None
    # S3 credentials (``s3`` only); ``None`` means "use the boto3 credential chain".
    access_key: str | None = None
    secret_key: SecretStr | None = None
    # Audit archive bucket of the ``s3`` backend (``minio`` uses ``MINIO_AUDIT_ARCHIVE_BUCKET``);
    # ``None`` falls back to the main bucket. Keys always live under ``audit/``.
    audit_archive_bucket: str | None = None

    @field_validator(
        "endpoint",
        "region",
        "bucket",
        "access_key",
        "secret_key",
        "audit_archive_bucket",
        mode="before",
    )
    @classmethod
    def _empty_storage_field_to_none(cls, value: object) -> object:
        return _normalise_empty_secret(value)

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(
                "STORAGE_ENDPOINT must be a full URL with an http:// or https:// scheme and a "
                f"host (e.g. 'https://s3.example.com'), got {value!r}"
            )
        return value

    @property
    def endpoint_url(self) -> str | None:
        """Return the explicit S3 endpoint URL, if configured."""
        return self.endpoint

    def resolved_bucket(self, minio: MinioSettings) -> str:
        """Return ``STORAGE_BUCKET`` when set, otherwise ``MINIO_BUCKET`` of ``minio``."""
        return self.bucket if self.bucket else minio.bucket

    def resolved_audit_archive_bucket(self, minio: MinioSettings) -> str:
        """Return the audit archive bucket of the active backend.

        ``minio`` -> ``MINIO_AUDIT_ARCHIVE_BUCKET``, ``s3`` -> ``STORAGE_AUDIT_ARCHIVE_BUCKET``;
        unset or blank -> the main bucket (:meth:`resolved_bucket`).
        """
        explicit = (
            minio.audit_archive_bucket if self.backend == "minio" else self.audit_archive_bucket
        )
        if explicit is not None and explicit.strip():
            return explicit.strip()
        return self.resolved_bucket(minio)


class LogsSettings(BaseSettings):
    """Click-log buffer and text files (``LOGS_*`` env variables).

    ``LOGS_DIR`` is the path *inside* the container (``/app/logs``). ``LOGS_DATA_DIR`` is a
    different variable: the *host* directory bind-mounted there by docker compose; it also
    reaches the container env through ``env_file`` but there is no ``data_dir`` field, so
    ``extra="ignore"`` drops it.
    """

    model_config = _base_config("LOGS_")

    dir: Path = Path("/app/logs")
    buffer_queue_size: int = Field(default=10_000, gt=0)
    buffer_batch_size: int = Field(default=500, gt=0)
    buffer_flush_interval_seconds: float = Field(default=1.0, gt=0)
    buffer_db_retry_attempts: int = Field(default=3, gt=0)
    buffer_shutdown_timeout_seconds: float = Field(default=10.0, gt=0)
    file_max_open: int = Field(default=256, gt=0)
    file_max_bytes: int = Field(default=50 * 1024 * 1024, gt=0)


# Networks where GlitchTip may live (SENTRY_DSN host); the same list is in
# ops/scripts/check_internal_url.sh. Explicit on purpose: ``ip_address.is_private`` also
# covers other special ranges (169.254/16, 2001:db8::/32, ...) and changes between Pythons.
_INTERNAL_NETWORKS: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "100.64.0.0/10",  # Tailscale CGNAT
        "::1/128",
        "fc00::/7",
    )
)
_SERVICE_NAME_RE: Final = re.compile(r"^[a-z0-9]([a-z0-9_-]{0,61}[a-z0-9])?$")
_HAS_LETTER_RE: Final = re.compile(r"[a-z]")
_HEX_RE: Final = re.compile(r"^0x[0-9a-f]*$")


def is_internal_host(host: str) -> bool:
    """Private/loopback/CGNAT IP or a name without dots (Docker service, ``localhost``)."""
    host = host.strip().lower().strip("[]")
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return _is_service_name(host)
    return any(addr in net for net in _INTERNAL_NETWORKS)


def _is_service_name(host: str) -> bool:
    """Docker service name: no dots, at least one letter, not a numeric IPv4 form.

    glibc ``getaddrinfo`` resolves ``134744072``, ``0x08080808``, ``017700000001`` (decimal/
    hex/octal ``inet_aton`` forms) to IP addresses, so they are rejected as names.
    """
    if not _SERVICE_NAME_RE.match(host) or not _HAS_LETTER_RE.search(host):
        return False
    if _HEX_RE.match(host):
        return False
    try:
        socket.inet_aton(host)
    except OSError:
        return True
    return False


class ObservabilitySettings(BaseSettings):
    """Health/metrics HTTP server and Sentry (GlitchTip) error reporting.

    HTTP server: ``OBSERVABILITY_HTTP_HOST`` / ``OBSERVABILITY_HTTP_PORT``, per-check
    timeout ``OBSERVABILITY_HEALTH_TIMEOUT_SECONDS``, heartbeat TTL
    ``OBSERVABILITY_HEARTBEAT_TTL_SECONDS``. Sentry uses the conventional names
    ``SENTRY_DSN`` (empty = disabled), ``SENTRY_ENVIRONMENT`` (empty = ``PROJECT_ENVIRONMENT``)
    and ``SENTRY_RELEASE`` (empty = ``tg-shop-platform@<version>``,
    see :mod:`app.core.version`). The prefix never clashes with ``OBS_*_DATA_DIR``
    (host directories of the observability stack, not read by the application).
    """

    model_config = _base_config("OBSERVABILITY_")
    # SENTRY_DSN carries the project key: never echo inputs in validation errors.
    model_config["hide_input_in_errors"] = True

    # Inside a container the server must listen on all interfaces (scraped over the network).
    http_host: str = "0.0.0.0"  # noqa: S104
    http_port: int = Field(default=8080, gt=0, lt=65536)
    health_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    heartbeat_ttl_seconds: int = Field(default=30, gt=0)
    # ``/health/ready`` reuses one report for this long (bounds the probe rate).
    ready_cache_ttl_seconds: float = Field(default=1.0, ge=0, le=60)
    sentry_dsn: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("SENTRY_DSN", "sentry_dsn")
    )
    sentry_environment: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SENTRY_ENVIRONMENT", "sentry_environment"),
    )
    sentry_release: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SENTRY_RELEASE", "sentry_release"),
    )

    @field_validator("sentry_dsn", "sentry_environment", "sentry_release", mode="before")
    @classmethod
    def _empty_sentry_to_none(cls, value: object) -> object:
        return _normalise_empty_secret(value)

    @field_validator("sentry_dsn", mode="after")
    @classmethod
    def _sentry_dsn_internal_only(cls, value: SecretStr | None) -> SecretStr | None:
        """GlitchTip is reachable only inside Docker/VPN: a public DSN host fails startup."""
        if value is None:
            return None
        parts = urlsplit(value.get_secret_value().strip())
        try:
            host = parts.hostname or ""
            _ = parts.port  # ValueError for a port outside 0..65535 / non-numeric
        except ValueError:
            host = ""
        if parts.scheme not in ("http", "https") or not is_internal_host(host):
            msg = (
                f"SENTRY_DSN must point to an internal GlitchTip address, got host {host!r}: "
                "use a private IP (10/8, 172.16/12, 192.168/16, 127/8, 100.64/10, ::1, fc00::/7) "
                "or a Docker service name without dots (http://<key>@glitchtip-web:8000/<id>)"
            )
            raise ValueError(msg)
        return value

    # OpenTelemetry tracing (app.core.telemetry). Empty endpoint or OTEL_SDK_DISABLED=true
    # -> tracing off (NoOp), the app works without a collector.
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OTEL_EXPORTER_OTLP_ENDPOINT", "otel_exporter_otlp_endpoint"),
    )
    otel_sdk_disabled: bool = Field(
        default=False, validation_alias=AliasChoices("OTEL_SDK_DISABLED", "otel_sdk_disabled")
    )
    # Empty = process name (master_bot / bot_worker).
    otel_service_name: str | None = Field(
        default=None, validation_alias=AliasChoices("OTEL_SERVICE_NAME", "otel_service_name")
    )
    otel_traces_sampler_ratio: float = Field(
        default=1.0,
        ge=0,
        le=1,
        validation_alias=AliasChoices(
            # Standard OTel name first; the older project names stay as aliases.
            "OTEL_TRACES_SAMPLER_ARG",
            "OTEL_TRACES_SAMPLER_RATIO",
            "OTEL_SAMPLE_RATIO",
            "otel_traces_sampler_ratio",
        ),
    )

    @field_validator("otel_exporter_otlp_endpoint", "otel_service_name", mode="before")
    @classmethod
    def _empty_otel_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("otel_exporter_otlp_endpoint", mode="after")
    @classmethod
    def _otlp_endpoint_internal_only(cls, value: str | None) -> str | None:
        """Alloy OTLP is reachable only inside Docker/VPN, like the Sentry DSN."""
        if value is None:
            return None
        parts = urlsplit(value.strip())
        try:
            host = parts.hostname or ""
            _ = parts.port
        except ValueError:
            host = ""
        if parts.scheme not in ("http", "https") or not is_internal_host(host):
            msg = (
                f"OTEL_EXPORTER_OTLP_ENDPOINT must be an internal http(s) URL, got host {host!r} "
                "(e.g. http://alloy:4317)"
            )
            raise ValueError(msg)
        return value.strip()


# ``Settings`` field name -> nested section class.
_NESTED_SECTIONS: Final[dict[str, type[BaseSettings]]] = {
    "postgres": PostgresSettings,
    "sqlalchemy": SQLAlchemySettings,
    "redis": RedisSettings,
    "rabbitmq": RabbitMQSettings,
    "minio": MinioSettings,
    "encryption": EncryptionSettings,
    "storage": StorageSettings,
    "logs": LogsSettings,
    "observability": ObservabilitySettings,
}


class Settings(BaseSettings):
    """Root application settings aggregated from the environment.

    Nested sections mirror ``POSTGRES_*``, ``SQLALCHEMY_*``, ``REDIS_*``, ``RABBITMQ_*``,
    ``MINIO_*``, ``ENCRYPTION_*`` and ``STORAGE_*`` variables. Type errors (e.g.
    ``POSTGRES_PORT=abc``) surface as ``ValidationError`` naming the offending field, raised
    when ``Settings()`` is constructed — never on import.
    """

    model_config = _base_config("", env_file=_DEFAULT_ENV_FILE)

    project_environment: ProjectEnvironment = "loc"
    project_name: str = "tg_shop_platform"

    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    sqlalchemy: SQLAlchemySettings = Field(default_factory=SQLAlchemySettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    rabbitmq: RabbitMQSettings = Field(default_factory=RabbitMQSettings)
    minio: MinioSettings = Field(default_factory=MinioSettings)
    encryption: EncryptionSettings = Field(default_factory=EncryptionSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    logs: LogsSettings = Field(default_factory=LogsSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    def __init__(self, **data: Any) -> None:
        """Initialise settings, forwarding the dotenv source to nested sections.

        pydantic-settings resolves nested ``BaseSettings`` fields through their own sources,
        so the root dotenv file would never reach them. Each section is therefore built here
        with the resolved file: ``_env_file`` not passed -> ``model_config["env_file"]``,
        ``None`` -> no file, a path -> that path.

        A section passed as a mapping keeps init > env > dotenv precedence for its other
        fields. A ready ``BaseSettings`` instance is used as is (caller's responsibility).
        """
        config = type(self).model_config
        env_file = data.get("_env_file", _UNSET)
        if env_file is _UNSET:
            env_file = config.get("env_file")
        encoding = data.get("_env_file_encoding") or config.get("env_file_encoding")
        for name, section_cls in _NESTED_SECTIONS.items():
            value = data.get(name)
            if value is None:
                data[name] = section_cls(_env_file=env_file, _env_file_encoding=encoding)
            elif isinstance(value, Mapping):
                data[name] = section_cls(
                    _env_file=env_file, _env_file_encoding=encoding, **dict(value)
                )
        super().__init__(**data)

    @property
    def database_url(self) -> str:
        """Build a ``postgresql+psycopg`` DSN with a properly encoded password."""
        pg = self.postgres
        if pg.password is None:
            raise ValueError("POSTGRES_PASSWORD is not configured")
        url = URL.create(
            drivername="postgresql+psycopg",
            username=pg.user,
            password=pg.password.get_secret_value(),
            host=pg.host,
            port=pg.port,
            database=pg.db,
        )
        return url.render_as_string(hide_password=False)

    @property
    def redis_url(self) -> str:
        """Build a ``redis://`` DSN, including credentials only when a password is set."""
        redis = self.redis
        password = redis.password.get_secret_value()
        auth = f":{quote(password, safe='')}@" if password else ""
        return f"redis://{auth}{redis.host}:{redis.port}/{redis.db}"

    @property
    def rabbitmq_url(self) -> str:
        """Build an ``amqp://`` DSN; the root vhost ``/`` is encoded as ``%2F``."""
        rabbit = self.rabbitmq
        password = quote(rabbit.password.get_secret_value(), safe="")
        user = quote(rabbit.user, safe="")
        vhost = quote(rabbit.vhost, safe="")
        return f"amqp://{user}:{password}@{rabbit.host}:{rabbit.port}/{vhost}"

    @property
    def minio_endpoint_url(self) -> str:
        """Build the MinIO endpoint URL with a scheme derived from ``MINIO_SECURE``."""
        scheme: Literal["http", "https"] = "https" if self.minio.secure else "http"
        return f"{scheme}://{self.minio.endpoint}"

    @property
    def encryption_key_list(self) -> list[str]:
        """Return the effective Fernet key list: current key first, then old keys."""
        keys: list[str] = []
        if self.encryption.key is not None:
            current = self.encryption.key.get_secret_value().strip()
            if current:
                keys.append(current)
        keys.extend(self.encryption.keys_old_list)
        return keys


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    return Settings()
