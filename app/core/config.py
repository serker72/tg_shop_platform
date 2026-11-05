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

import re
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Final, Literal
from urllib.parse import quote

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

__all__ = [
    "EncryptionSettings",
    "MinioSettings",
    "PostgresSettings",
    "RabbitMQSettings",
    "RedisSettings",
    "SQLAlchemySettings",
    "Settings",
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
    def _empty_to_none(cls, value: object) -> object:
        return _normalise_empty_secret(value)

    @property
    def keys_old_list(self) -> list[str]:
        """Return the parsed list of previous keys."""
        if self.keys_old is None:
            return []
        raw = self.keys_old.get_secret_value()
        return [item.strip() for item in raw.split(",") if item.strip()]


# ``Settings`` field name -> nested section class.
_NESTED_SECTIONS: Final[dict[str, type[BaseSettings]]] = {
    "postgres": PostgresSettings,
    "sqlalchemy": SQLAlchemySettings,
    "redis": RedisSettings,
    "rabbitmq": RabbitMQSettings,
    "minio": MinioSettings,
    "encryption": EncryptionSettings,
}


class Settings(BaseSettings):
    """Root application settings aggregated from the environment.

    Nested sections mirror ``POSTGRES_*``, ``SQLALCHEMY_*``, ``REDIS_*``, ``RABBITMQ_*``,
    ``MINIO_*`` and ``ENCRYPTION_*`` variables. Type errors (e.g. ``POSTGRES_PORT=abc``)
    surface as ``ValidationError`` naming the offending field, raised when ``Settings()``
    is constructed — never on import.
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
