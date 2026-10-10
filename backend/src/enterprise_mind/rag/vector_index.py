import hashlib
import re

from sqlalchemy import URL, create_engine, text
from sqlalchemy.pool import NullPool

from enterprise_mind.infrastructure.provision import MIGRATOR, OWNER
from enterprise_mind.infrastructure.settings import ConfigurationError
from enterprise_mind.rag.config import EmbeddingConfig, HNSWConfig


def model_literal(model_key: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,200}", model_key):
        raise ConfigurationError("模型稳定键不合法。")
    return "'" + model_key + "'"


def index_name(model_key: str, dimensions: int) -> str:
    return (
        "ix_embeddings_hnsw_"
        + hashlib.sha256(f"{model_key}:{dimensions}".encode()).hexdigest()[:16]
    )


def build_vector_index(url: URL, model: EmbeddingConfig, config: HNSWConfig) -> None:
    if model.dimensions > 2000:
        raise ConfigurationError("当前 vector HNSW 上限为 2000 维；该模型继续使用精确检索。")
    engine = create_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        with engine.begin() as connection:
            if url.username == MIGRATOR:
                connection.execute(text(f"SET LOCAL ROLE {OWNER}"))
            connection.execute(text("SET LOCAL lock_timeout='10s'"))
            connection.execute(text("SET LOCAL statement_timeout='120s'"))
            connection.execute(text("SELECT pg_advisory_xact_lock(4617078203315146320)"))
            connection.execute(
                text(f"""
CREATE INDEX IF NOT EXISTS {index_name(model.model_key, model.dimensions)}
ON knowledge.chunk_embeddings
USING hnsw ((embedding::vector({model.dimensions})) vector_cosine_ops)
WITH (m={config.m}, ef_construction={config.ef_construction})
WHERE model_key={model_literal(model.model_key)}
    AND dimensions={model.dimensions} AND status='ready'
""")
            )
    finally:
        engine.dispose()
