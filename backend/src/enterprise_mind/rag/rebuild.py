"""Offline embedding cutover using existing rows and an atomic configuration replacement."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import yaml
from sqlalchemy import URL, Connection, create_engine, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from enterprise_mind.infrastructure.provision import MIGRATOR, OWNER
from enterprise_mind.infrastructure.settings import ConfigurationError
from enterprise_mind.ingestion.models import Revision
from enterprise_mind.rag.config import EmbeddingConfig, ProvidersConfig, RAGSettings, load_rag
from enterprise_mind.rag.models import Chunk, ChunkEmbedding
from enterprise_mind.rag.providers import EmbeddingProvider, ProviderFailure, validate_vector

LOCK = 4617078203315146321


@contextmanager
def maintenance_connection(url: URL) -> Iterator[Connection]:
    engine = create_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            if not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK}):
                raise ConfigurationError("已有 Embedding 重建或切换正在执行。")
            connection.commit()
            # NullPool closes the physical session and releases its advisory lock on every exit.
            yield connection
    finally:
        engine.dispose()


def transaction_role(session: Session, url: URL) -> None:
    if url.username == MIGRATOR:
        session.execute(text(f"SET LOCAL ROLE {OWNER}"))
    session.execute(text("SET LOCAL lock_timeout='5s'"))
    session.execute(text("SET LOCAL statement_timeout='60s'"))


def ready_chunks() -> Any:
    # Include the recycle bin: restoring a retained revision must keep its vector coverage.
    return (
        select(Chunk)
        .join(Revision, Revision.id == Chunk.revision_id)
        .where(Chunk.status == "ready", Revision.retired_at.is_(None))
    )


def target_vector(model: EmbeddingConfig) -> Any:
    return (
        select(ChunkEmbedding.id)
        .where(
            ChunkEmbedding.chunk_id == Chunk.id,
            ChunkEmbedding.model_key == model.model_key,
            ChunkEmbedding.dimensions == model.dimensions,
            ChunkEmbedding.status == "ready",
        )
        .exists()
    )


def remaining(session: Session, model: EmbeddingConfig) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(ready_chunks().where(~target_vector(model)).subquery())
        )
        or 0
    )


async def rebuild_embeddings(
    url: URL, model: EmbeddingConfig, provider: EmbeddingProvider
) -> dict[str, int]:
    if provider.model_key != model.model_key or provider.dimensions != model.dimensions:
        raise ConfigurationError("目标模型与重建 Provider 不一致。")
    written, cursor = 0, 0
    with maintenance_connection(url) as connection:
        with Session(connection) as session, session.begin():
            transaction_role(session, url)
            if session.scalar(
                select(ChunkEmbedding.id)
                .where(
                    ChunkEmbedding.model_key == model.model_key,
                    ChunkEmbedding.dimensions != model.dimensions,
                )
                .limit(1)
            ):
                raise ConfigurationError("目标模型键已有不同维度，必须使用新的稳定键。")
            high_water = session.scalar(select(func.max(Chunk.id))) or 0
        while True:
            with Session(connection) as session, session.begin():
                transaction_role(session, url)
                rows = session.scalars(
                    ready_chunks()
                    .where(Chunk.id > cursor, Chunk.id <= high_water, ~target_vector(model))
                    .order_by(Chunk.id)
                    .limit(model.max_batch_size)
                ).all()
                batch = [(row.id, row.content) for row in rows]
            if not batch:
                break
            if any(
                provider.counter.count(content) > model.max_input_tokens for _, content in batch
            ):
                raise ConfigurationError("已有完整 Chunk 超出目标模型输入上限，未改变当前模型。")
            # No database transaction is open while original text is sent to the configured model.
            vectors = await provider.embed([content for _, content in batch])
            if len(vectors) != len(batch):
                raise ProviderFailure("EMBEDDING_INVALID")
            vectors = [validate_vector(vector, model.dimensions) for vector in vectors]
            with Session(connection) as session, session.begin():
                transaction_role(session, url)
                for (identifier, content), vector in zip(batch, vectors, strict=True):
                    chunk = session.scalar(
                        ready_chunks().where(Chunk.id == identifier).with_for_update(of=Chunk)
                    )
                    if chunk is None or chunk.content != content:
                        # Deletion or retirement during the provider call must not be reversed.
                        continue
                    values = {
                        "chunk_id": identifier,
                        "model_key": model.model_key,
                        "dimensions": model.dimensions,
                        "embedding": vector,
                        "status": "ready",
                        "embedded_at": datetime.now(UTC),
                        "error_code": None,
                    }
                    session.execute(
                        insert(ChunkEmbedding)
                        .values(**values)
                        .on_conflict_do_update(constraint="uq_chunk_embeddings_model", set_=values)
                    )
                    written += 1
            cursor = batch[-1][0]
        with Session(connection) as session, session.begin():
            transaction_role(session, url)
            return {"written": written, "remaining": remaining(session, model)}


def change_profiles(
    settings: RAGSettings, target_path: Path
) -> tuple[ProvidersConfig, ProvidersConfig, bytes]:
    before = settings.providers_config.read_bytes()
    _, current = load_rag(settings)
    _, target = load_rag(settings.model_copy(update={"providers_config": target_path}))
    if current.embedding is None or target.embedding is None:
        raise ConfigurationError("切换需要显式配置当前与目标 Embedding。")
    if current.embedding.model_key == target.embedding.model_key:
        raise ConfigurationError("目标 Embedding 必须使用不同的稳定模型键。")
    if current.model_dump(exclude={"embedding", "allowed_origins"}) != target.model_dump(
        exclude={"embedding", "allowed_origins"}
    ):
        raise ConfigurationError("Embedding 运维不能同时改变其他模型配置。")
    if settings.providers_config.read_bytes() != before:
        raise ConfigurationError("读取期间配置已变化，请重新执行。")
    return current, target, before


def activate_embeddings(url: URL, settings: RAGSettings, target_path: Path) -> Path:
    current, target, before = change_profiles(settings, target_path)
    assert current.embedding and target.embedding
    path = settings.providers_config.absolute()
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError("仅可原子替换普通 Provider 配置文件。")
    backup = path.with_name(path.name + ".before-" + target.embedding.model_key)
    temporary: Path | None = None
    with maintenance_connection(url) as connection, Session(connection) as session, session.begin():
        transaction_role(session, url)
        # The runtime is intentionally file-configured. All API/Worker/Dispatcher processes
        # must be stopped for cutover; this is not a hot-reload or a distributed model switch.
        if session.scalar(
            text("""
SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database()
  AND pid<>pg_backend_pid() AND application_name LIKE 'enterprise-mind-%')
""")
        ):
            raise ConfigurationError("切换前必须停止本库的 API、Worker 和 Dispatcher。")
        session.execute(
            text("""
LOCK TABLE knowledge.documents, knowledge.document_revisions, knowledge.processing_tasks,
    knowledge.chunks, knowledge.chunk_embeddings IN SHARE MODE
""")
        )
        if session.scalar(
            text("""
SELECT EXISTS (SELECT 1 FROM knowledge.processing_tasks WHERE status IN ('pending','processing'))
""")
        ):
            raise ConfigurationError("仍有文档处理任务未完成，请先排空再切换。")
        if remaining(session, target.embedding):
            raise ConfigurationError("目标模型向量覆盖不完整，请先完成重建。")
        if path.read_bytes() != before:
            raise ConfigurationError("当前配置已变化，请重新执行。")
        payload = yaml.safe_load(before)
        payload["embedding"] = target.embedding.model_dump(mode="json")
        payload["allowed_origins"] = sorted(set(current.allowed_origins + target.allowed_origins))
        # Other tokenizer paths also need to stay relative to the unchanged active profile.
        output = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8")
        try:
            if backup.exists():
                if backup.read_bytes() != before:
                    raise ConfigurationError("旧配置备份已存在且不同，未覆盖备份或当前配置。")
            else:
                with backup.open("xb") as handle:
                    handle.write(before)
                    handle.flush()
                    os.fsync(handle.fileno())
            with NamedTemporaryFile(dir=path.parent, prefix=".embedding-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(output)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, path.stat().st_mode)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return backup
