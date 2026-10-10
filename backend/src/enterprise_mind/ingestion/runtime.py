"""Separate dispatcher and worker entrypoints; PostgreSQL owns all durable state."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from enterprise_mind.database.config import load_database
from enterprise_mind.database.mcp_client import create_client
from enterprise_mind.database.service import poll_schema_refresh_outbox
from enterprise_mind.infrastructure.database import check_database, create_engine, session_factory
from enterprise_mind.infrastructure.provision import APP
from enterprise_mind.infrastructure.settings import (
    ApplicationConfig,
    ConfigurationError,
    DatabaseSettings,
    IngestionSettings,
)
from enterprise_mind.ingestion.dispatch import Dispatcher, sweep_orphans
from enterprise_mind.ingestion.media import MediaService
from enterprise_mind.ingestion.media_providers import HTTPVision
from enterprise_mind.ingestion.office import OfficeConverter
from enterprise_mind.ingestion.provider_factory import create_ocr_provider
from enterprise_mind.ingestion.queue import TaskQueue
from enterprise_mind.ingestion.storage import ObjectStorage
from enterprise_mind.ingestion.worker import IngestionWorker
from enterprise_mind.rag.config import RAGSettings, load_rag
from enterprise_mind.rag.indexing import IndexingService
from enterprise_mind.rag.providers import HTTPEmbeddings

SCHEDULER_LOCK = 4617078203315146311


@asynccontextmanager
async def _scheduler_slot(engine: AsyncEngine) -> AsyncIterator[AsyncConnection | None]:
    async with engine.connect() as connection:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": SCHEDULER_LOCK}
        )
        await connection.commit()
        try:
            yield connection if acquired else None
        finally:
            if acquired:
                # Close the physical session: a pooled return must not retain the lock.
                await connection.invalidate()


def _connection_failure(error: DBAPIError) -> bool:
    # SQLAlchemy's asyncpg adapter retains PostgreSQL's SQLSTATE. Statement,
    # permission and integrity failures must still terminate the dispatcher.
    sqlstate = getattr(error.orig, "sqlstate", None)
    return error.connection_invalidated or (
        isinstance(sqlstate, str)
        and (sqlstate.startswith("08") or sqlstate in {"57P01", "57P02", "57P03"})
    )


async def run_processing(
    command: str, settings: DatabaseSettings, config: ApplicationConfig, *, once: bool
) -> None:
    if settings.env == "production" and settings.postgres_user != APP:
        raise ConfigurationError("生产任务进程必须使用专用运行账号。")
    services = IngestionSettings()
    engine = create_engine(
        settings, config, role="scheduler" if command == "dispatch" else "worker"
    )
    sessions = session_factory(engine)
    database_config = load_database() if command == "worker" else None
    database_client = create_client(database_config, sessions) if database_config else None
    queue = TaskQueue(services)
    storage = ObjectStorage(services, config.storage)
    try:
        await check_database(engine)
        await asyncio.to_thread(storage.check)
        if command == "dispatch":
            dispatcher = Dispatcher(sessions, queue, config)
            next_sweep = 0.0
            while True:
                try:
                    async with _scheduler_slot(engine) as leadership:
                        if leadership is not None:
                            leader_pid = await leadership.scalar(text("SELECT pg_backend_pid()"))
                            await leadership.commit()
                            while True:
                                # A lost/reconnected session no longer owns the session lock.
                                current_pid = await leadership.scalar(
                                    text("SELECT pg_backend_pid()")
                                )
                                await leadership.commit()
                                if current_pid != leader_pid:
                                    raise ConnectionError("Scheduler lock connection changed.")
                                await dispatcher.dispatch_once()
                                if monotonic() >= next_sweep:
                                    await sweep_orphans(
                                        sessions, storage, config.storage.orphan_grace_seconds
                                    )
                                    next_sweep = monotonic() + 3600
                                if once:
                                    return
                                await asyncio.sleep(config.outbox.poll_interval_seconds)
                except (TimeoutError, ConnectionError, PoolTimeoutError):
                    if once:
                        raise
                except DBAPIError as error:
                    if once or not _connection_failure(error):
                        raise
                if once:
                    return
                await asyncio.sleep(config.outbox.poll_interval_seconds)
        else:
            rag_config, providers = load_rag(RAGSettings())
            indexing = (
                IndexingService(sessions, rag_config, HTTPEmbeddings(providers.embedding))
                if providers.embedding
                else None
            )
            media = MediaService(
                sessions,
                storage,
                config.worker,
                create_ocr_provider(providers.ocr),
                HTTPVision(providers.vision) if providers.vision else None,
            )
            converter = (
                OfficeConverter(config.office_converter, config.worker.max_artifact_bytes)
                if config.office_converter
                else None
            )

            async def consume() -> None:
                worker = IngestionWorker(
                    sessions,
                    storage,
                    queue,
                    config.worker,
                    indexing,
                    media,
                    converter,
                    config.phase9,
                    database=database_client,
                )
                while True:
                    try:
                        await worker.run_once()
                    except Exception:
                        if once:
                            raise
                        # A lost transport or process does not consume durable work.
                        await asyncio.sleep(config.outbox.poll_interval_seconds)
                    if once:
                        return

            async def refresh_schemas() -> None:
                assert database_client is not None
                while True:
                    try:
                        async with sessions() as session, session.begin():
                            await poll_schema_refresh_outbox(
                                session,
                                database_client.fetch_schema,
                                limit=1,
                            )
                    except Exception:
                        if once:
                            raise
                    if once:
                        return
                    await asyncio.sleep(config.outbox.poll_interval_seconds)

            async with asyncio.TaskGroup() as group:
                if database_client is not None:
                    group.create_task(refresh_schemas())
                for _ in range(1 if once else config.worker.concurrency):
                    group.create_task(consume())
    finally:
        if database_client is not None:
            await database_client.close()
        await queue.close()
        await engine.dispose()
