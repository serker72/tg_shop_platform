"""AWS S3 backend (also works with any S3-compatible service).

Virtual-hosted-style addressing and the configured region are the only differences from
:class:`~app.shared_kernel.infrastructure.storage.boto3_storage.Boto3StorageService`.
"""

from __future__ import annotations

from typing import Any

# ``boto3``/``botocore`` are shipped without type stubs (no ``py.typed``), so mypy would skip
# their analysis and also skip every module importing them. The explicit ignore keeps the rest
# of these modules fully checked; the client/client-error objects are therefore typed ``Any``.
import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

from app.core.config import Settings
from app.shared_kernel.infrastructure.storage.boto3_storage import (
    MAX_POOL_CONNECTIONS,
    Boto3StorageService,
)

__all__ = ["S3Storage", "build_s3_client", "s3_client_config"]

_DEFAULT_REGION = "us-east-1"


def s3_client_config() -> Config:
    """Return the ``botocore`` config for an AWS S3 (or S3-compatible) client."""
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": "virtual"},
        retries={"max_attempts": 3, "mode": "standard"},
        connect_timeout=10,
        read_timeout=120,
        max_pool_connections=MAX_POOL_CONNECTIONS,
    )


def build_s3_client(settings: Settings) -> Any:
    """Create a ``boto3`` S3 client for AWS or an S3-compatible endpoint.

    Credentials come only from ``STORAGE_ACCESS_KEY``/``STORAGE_SECRET_KEY``; ``MINIO_*``
    credentials are never used for S3. When they are not set, ``None`` is passed and boto3
    resolves credentials through its standard provider chain (env ``AWS_*``, shared
    config/profile, IAM role).

    Returns ``Any``: ``botocore``/``boto3`` publish no type stubs.
    """
    storage = settings.storage
    secret_key = storage.secret_key.get_secret_value() if storage.secret_key is not None else None
    return boto3.client(
        "s3",
        endpoint_url=storage.endpoint_url,
        aws_access_key_id=storage.access_key,
        aws_secret_access_key=secret_key,
        region_name=storage.region or _DEFAULT_REGION,
        config=s3_client_config(),
    )


class S3Storage(Boto3StorageService):
    """``StorageService`` bound to an AWS S3 (or S3-compatible) bucket.

    ``bucket`` overrides the configured main bucket (e.g. the audit archive bucket).
    """

    def __init__(self, *, client: Any, settings: Settings, bucket: str | None = None) -> None:
        super().__init__(
            client=client,
            bucket=bucket or settings.storage.resolved_bucket(settings.minio),
            endpoint_url=settings.storage.endpoint_url,
        )
