"""Dishka providers of the shared kernel.

Providers never call ``get_settings()`` themselves: ``Settings`` is injected as a dependency
so any container can be assembled with a custom settings instance in tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Iterable
from typing import NewType, assert_never

from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.shared_kernel.domain.storage import StorageService
from app.shared_kernel.infrastructure.database import build_engine, build_session_factory
from app.shared_kernel.infrastructure.storage.minio import MinIOStorage, build_minio_client
from app.shared_kernel.infrastructure.storage.s3 import S3Storage, build_s3_client

__all__ = ["DatabaseProvider", "StorageClient", "StorageProvider"]

# Explicit DI key of the raw ``boto3`` S3 client. ``botocore`` ships no type stubs, so the
# client has no importable type; a ``NewType`` gives dishka a dedicated key instead of ``Any``.
StorageClient = NewType("StorageClient", object)


class StorageProvider(Provider):
    """Bind ``StorageService`` to the backend chosen by ``STORAGE_BACKEND``.

    Both the raw ``boto3`` client (:data:`StorageClient`) and the storage service are
    application-scoped singletons: a client carries a connection pool and must not be
    re-created per request. The client is closed when the container is closed.
    """

    scope = Scope.APP

    @provide
    def provide_storage_client(self, settings: Settings) -> Iterable[StorageClient]:
        """Create the ``boto3`` client of the configured backend; close it on shutdown."""
        backend = settings.storage.backend
        match backend:
            case "minio":
                client = build_minio_client(settings)
            case "s3":
                client = build_s3_client(settings)
            case _:
                assert_never(backend)
        try:
            yield StorageClient(client)
        finally:
            client.close()

    @provide
    def provide_storage(self, settings: Settings, client: StorageClient) -> StorageService:
        """Return the ``StorageService`` implementation for the configured backend."""
        backend = settings.storage.backend
        match backend:
            case "minio":
                return MinIOStorage(client=client, settings=settings)
            case "s3":
                return S3Storage(client=client, settings=settings)
            case _:
                assert_never(backend)


class DatabaseProvider(Provider):
    """Async SQLAlchemy engine, session factory and per-request session.

    The engine (connection pool) and the session factory are application singletons; the
    engine is disposed when the container is closed. A session lives for one REQUEST scope.
    """

    @provide(scope=Scope.APP)
    async def provide_engine(self, settings: Settings) -> AsyncIterable[AsyncEngine]:
        """Create the engine; dispose its pool on container shutdown."""
        engine = build_engine(settings)
        try:
            yield engine
        finally:
            await engine.dispose()

    @provide(scope=Scope.APP)
    def provide_session_factory(self, engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        """Return the project session factory (``expire_on_commit=False``)."""
        return build_session_factory(engine)

    @provide(scope=Scope.REQUEST)
    async def provide_session(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> AsyncIterable[AsyncSession]:
        """Yield a session for one request and close it when the scope exits.

        Committing is the caller's job (unit of work): nothing is committed here. Closing
        the session (``AsyncSession.__aexit__`` -> ``close()``) rolls back any transaction
        still in progress and returns the connection to the pool - this is what discards
        uncommitted work after an exception, since dishka finalises generators without
        re-raising the error inside them.
        """
        async with factory() as session:
            yield session
