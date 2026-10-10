from typing import Literal

from sqlalchemy import Connection, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from enterprise_mind.identity.models import User
from enterprise_mind.infrastructure.settings import ApplicationConfig, DatabaseSettings

SCHEMA_REVISION = "0016_database_csv_export"
PG_VERSION = 180006
VECTOR_VERSION = "0.8.6"


class DatabaseNotReady(Exception):
    pass


def create_engine(
    settings: DatabaseSettings,
    config: ApplicationConfig,
    *,
    role: Literal["api", "worker", "scheduler"] = "api",
) -> AsyncEngine:
    pool_limit = {
        "api": config.postgres.api_pool_max_per_process,
        "worker": config.postgres.worker_pool_max,
        "scheduler": config.postgres.scheduler_pool_max,
    }[role]
    return create_async_engine(
        settings.database_url(asynchronous=True),
        pool_size=pool_limit,
        max_overflow=0,
        pool_timeout=config.postgres.connection_timeout_seconds,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "timeout": config.postgres.connection_timeout_seconds,
            "server_settings": {
                "timezone": "UTC",
                "statement_timeout": str(config.postgres.statement_timeout_ms),
                "application_name": "enterprise-mind-" + role,
            },
        },
    )


def check_connection(connection: Connection) -> None:
    versions = (
        connection.execute(text("SELECT version_num FROM infra.alembic_version")).scalars().all()
    )
    extensions = dict(
        connection.execute(text("SELECT extname, extversion FROM pg_extension")).tuples().all()
    )
    server_version = int(connection.execute(text("SHOW server_version_num")).scalar_one())
    if (
        versions != [SCHEMA_REVISION]
        or extensions.get("vector") != VECTOR_VERSION
        or "pg_trgm" not in extensions
        or server_version != PG_VERSION
    ):
        raise DatabaseNotReady("数据库版本、扩展或迁移版本未就绪。")
    if connection.execute(select(User.id).where(User.role == "admin").limit(1)).scalar() is None:
        raise DatabaseNotReady("管理员初始化未完成。")


async def check_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.run_sync(check_connection)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
