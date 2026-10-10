import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from enterprise_mind.identity.models import Base
from enterprise_mind.ingestion.models import Timestamped


class Vector(UserDefinedType[list[float]]):
    """pgvector's text wire format; dimensions belong to the model, not this column."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "VECTOR"

    def bind_processor(self, dialect: Dialect) -> Callable[[Any], str]:
        return lambda value: json.dumps(value, allow_nan=False)

    def result_processor(self, dialect: Dialect, coltype: object) -> Callable[[str], list[float]]:
        def decode(value: str) -> list[float]:
            return [float(item) for item in json.loads(value)]

        return decode


ischema_names["vector"] = Vector


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "document_id"],
            ["knowledge.document_revisions.id", "knowledge.document_revisions.document_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["document_id", "knowledge_base_id"],
            ["knowledge.documents.id", "knowledge.documents.knowledge_base_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("revision_id", "ordinal", name="uq_chunks_ordinal"),
        CheckConstraint("ordinal >= 0 AND token_count > 0", name="ck_chunks_counts"),
        CheckConstraint(
            "chunk_type IN ('text','table','code','image','mixed')", name="ck_chunks_type"
        ),
        CheckConstraint(
            "status IN ('pending','ready','failed','retired')", name="ck_chunks_status"
        ),
        CheckConstraint("length(content) > 0 AND length(search_tokens) > 0", name="ck_chunks_text"),
        Index(
            "ix_chunks_search",
            "search_vector",
            postgresql_using="gin",
            postgresql_where=text("status = 'ready'"),
        ),
        {"schema": "knowledge"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    revision_id: Mapped[int] = mapped_column(BigInteger)
    document_id: Mapped[int] = mapped_column(BigInteger)
    knowledge_base_id: Mapped[int] = mapped_column(BigInteger)
    ordinal: Mapped[int] = mapped_column(Integer)
    chunk_type: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(Text)
    search_tokens: Mapped[str] = mapped_column(Text)
    search_vector: Mapped[Any] = mapped_column(TSVECTOR, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer)
    context_group: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChunkAnchor(Base):
    __tablename__ = "chunk_anchors"
    __table_args__ = (
        UniqueConstraint("chunk_id", "anchor_id", name="uq_chunk_anchors_anchor"),
        UniqueConstraint("chunk_id", "ordinal", name="uq_chunk_anchors_ordinal"),
        CheckConstraint(
            "ordinal >= 0 AND content_start >= 0 AND content_end > content_start",
            name="ck_chunk_anchors_range",
        ),
        Index("ix_chunk_anchors_anchor", "anchor_id"),
        {"schema": "knowledge"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    chunk_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge.chunks.id", ondelete="CASCADE")
    )
    anchor_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge.source_anchors.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    content_start: Mapped[int] = mapped_column(Integer)
    content_end: Mapped[int] = mapped_column(Integer)
    quote_text: Mapped[str] = mapped_column(Text)


class ChunkEmbedding(Timestamped, Base):
    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        UniqueConstraint("chunk_id", "model_key", name="uq_chunk_embeddings_model"),
        CheckConstraint(
            "dimensions > 0 AND vector_dims(embedding) = dimensions AND vector_norm(embedding) > 0",
            name="ck_embeddings_dimensions",
        ),
        CheckConstraint("status IN ('ready','failed','retired')", name="ck_embeddings_status"),
        {"schema": "knowledge"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    chunk_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge.chunks.id", ondelete="CASCADE")
    )
    model_key: Mapped[str] = mapped_column(String(200))
    dimensions: Mapped[int] = mapped_column(SmallInteger)
    embedding: Mapped[list[float]] = mapped_column(Vector())
    status: Mapped[str] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(100))
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
