"""Object storage backends (MinIO, AWS S3) and the Dishka provider wiring them."""

from __future__ import annotations

from app.shared_kernel.infrastructure.storage.boto3_storage import Boto3StorageService
from app.shared_kernel.infrastructure.storage.minio import (
    MinIOStorage,
    build_minio_client,
    minio_client_config,
)
from app.shared_kernel.infrastructure.storage.s3 import S3Storage, build_s3_client, s3_client_config

__all__ = [
    "Boto3StorageService",
    "MinIOStorage",
    "S3Storage",
    "build_minio_client",
    "build_s3_client",
    "minio_client_config",
    "s3_client_config",
]
