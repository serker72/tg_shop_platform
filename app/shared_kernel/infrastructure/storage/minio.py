"""MinIO backend: the default object storage of the platform.

MinIO speaks the S3 API but serves buckets from a host-style-agnostic endpoint, so the
client uses path-style addressing and signature v4. Only client configuration differs from
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

__all__ = ["MinIOStorage", "build_minio_client", "minio_client_config"]


def minio_client_config(
    *, connect_timeout: float = 10, read_timeout: float = 60, max_retries: int = 3
) -> Config:
    """Return the ``botocore`` config for a MinIO client.

    ``max_retries`` excludes the initial call (``0`` = a single attempt);
    health probes use short timeouts and a single attempt.
    """
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        retries={"max_attempts": max_retries, "mode": "standard"},
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        max_pool_connections=MAX_POOL_CONNECTIONS,
    )


def build_minio_client(settings: Settings, *, config: Config | None = None) -> Any:
    """Create a ``boto3`` S3 client pointed at MinIO.

    Returns ``Any``: ``botocore``/``boto3`` publish no type stubs, so the concrete
    ``botocore.client.S3`` type cannot be imported under ``mypy --strict``.
    """
    minio = settings.minio
    secret_key = minio.secret_key.get_secret_value() if minio.secret_key is not None else None
    return boto3.client(
        "s3",
        endpoint_url=settings.minio_endpoint_url,
        aws_access_key_id=minio.access_key,
        aws_secret_access_key=secret_key,
        region_name=settings.storage.region or "us-east-1",
        config=config if config is not None else minio_client_config(),
    )


class MinIOStorage(Boto3StorageService):
    """``StorageService`` bound to a MinIO bucket (``MINIO_*`` settings).

    ``bucket`` overrides the configured main bucket (e.g. the audit archive bucket).
    """

    def __init__(self, *, client: Any, settings: Settings, bucket: str | None = None) -> None:
        super().__init__(
            client=client,
            bucket=bucket or settings.storage.resolved_bucket(settings.minio),
            endpoint_url=settings.minio_endpoint_url,
        )
