import asyncio
import os
import secrets
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from enterprise_mind.identity.models import User
from enterprise_mind.infrastructure.migrate import migrate
from enterprise_mind.infrastructure.settings import (
    APISettings,
    ApplicationConfig,
    IngestionSettings,
    read_secret,
)
from enterprise_mind.ingestion.storage import initialize_storage

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


@dataclass
class TestDatabase:
    url: URL
    engine: Engine
    settings: APISettings
    admin_password: str
    admin_secret: Path

    def initialize(self) -> bool:
        return migrate(self.url, ApplicationConfig(), BACKEND)

    def user_id(self) -> int:
        with Session(self.engine) as session:
            return session.query(User.id).filter(User.role == "admin").one()[0]


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestDatabase]:
    secret_path = os.getenv("EM_TEST_POSTGRES_PASSWORD_FILE")
    if not secret_path:
        pytest.skip("Set EM_TEST_POSTGRES_PASSWORD_FILE for the isolated PostgreSQL fixture")
    maintenance_url = URL.create(
        "postgresql+psycopg",
        username="em_test_owner",
        password=read_secret(Path(secret_path)).get_secret_value(),
        host="127.0.0.1",
        port=int(os.getenv("EM_TEST_POSTGRES_PORT", "55432")),
        database="em_stage1_test",
    )
    maintenance = create_engine(
        maintenance_url,
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
    )
    # Each test owns a new database; never truncate or drop the configured maintenance database.
    name = "em_test_" + uuid.uuid4().hex
    with maintenance.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = maintenance_url.set(database=name)
    engine = create_engine(
        url, hide_parameters=True, poolclass=NullPool, connect_args={"connect_timeout": 5}
    )
    admin_password = secrets.token_urlsafe(24)
    admin_secret = tmp_path / "admin_secret"
    admin_secret.write_text(admin_password, encoding="utf-8")
    token_secret = tmp_path / "token_secret"
    token_secret.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    monkeypatch.setenv("EM_BOOTSTRAP_ADMIN_USERNAME", "初始管理员")
    monkeypatch.setenv("EM_BOOTSTRAP_ADMIN_PASSWORD_FILE", str(admin_secret))
    settings = APISettings(
        env="test",
        postgres_host="127.0.0.1",
        postgres_port=maintenance_url.port,
        postgres_database=name,
        postgres_user="em_test_owner",
        postgres_password_file=Path(secret_path),
        token_signing_key_file=token_secret,
        application_config=ROOT / "config" / "application.yaml",
    )
    try:
        yield TestDatabase(url, engine, settings, admin_password, admin_secret)
    finally:
        engine.dispose()
        with maintenance.connect() as connection:
            # Only the random exact database created above is removed.
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        maintenance.dispose()


@pytest.fixture
def ingestion_settings(monkeypatch):
    if os.getenv("EM_TEST_INGESTION") != "1":
        pytest.skip("Set EM_TEST_INGESTION=1 for dedicated Redis and MinIO fixtures")
    from minio import Minio
    from redis import Redis

    if sys.platform == "win32":
        from enterprise_mind.ingestion import media, preview_rebuild, worker
        from enterprise_mind.ingestion.process import run_parser

        async def parser_on_worker_loop(*args, **kwargs):
            # The in-process API portal uses Selector for psycopg; production
            # parser work runs in a separate Worker with a subprocess-capable loop.
            # Keep the real credential-free parser child, including its limits.
            if isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop):

                async def parse():
                    return await run_parser(*args, **kwargs)

                def run():
                    with asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner:
                        return runner.run(parse())

                return await asyncio.to_thread(run)
            return await run_parser(*args, **kwargs)

        monkeypatch.setattr(media, "run_parser", parser_on_worker_loop)
        monkeypatch.setattr(preview_rebuild, "run_parser", parser_on_worker_loop)
        monkeypatch.setattr(worker, "run_parser", parser_on_worker_loop)

    secrets_dir = ROOT / ".local" / "test-secrets"
    settings = IngestionSettings(
        minio_endpoint="127.0.0.1:" + os.getenv("EM_TEST_MINIO_PORT", "59000"),
        minio_bucket="em-test-" + uuid.uuid4().hex,
        minio_access_key_file=secrets_dir / "minio_app_access",
        minio_secret_key_file=secrets_dir / "minio_app_secret",
        redis_host="127.0.0.1",
        redis_port=int(os.getenv("EM_TEST_REDIS_PORT", "56379")),
        redis_password_file=secrets_dir / "redis_password",
        redis_stream="enterprise-mind:test:" + uuid.uuid4().hex,
    )
    initialize_storage(
        settings,
        ApplicationConfig().storage,
        secrets_dir / "minio_root_access",
        secrets_dir / "minio_root_secret",
    )
    root_storage = Minio(
        settings.minio_endpoint,
        secure=False,
        access_key=read_secret(secrets_dir / "minio_root_access").get_secret_value(),
        secret_key=read_secret(secrets_dir / "minio_root_secret").get_secret_value(),
    )
    root_queue = Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        username="test_admin",
        password=read_secret(secrets_dir / "redis_test_admin").get_secret_value(),
        decode_responses=True,
    )
    try:
        yield settings, root_storage, root_queue
    finally:
        # Exact random resources created by this fixture; never affect other buckets or streams.
        for item in root_storage.list_objects(settings.minio_bucket, recursive=True):
            root_storage.remove_object(settings.minio_bucket, item.object_name)
        root_storage.remove_bucket(settings.minio_bucket)
        root_queue.delete(settings.redis_stream)
        root_queue.close()
