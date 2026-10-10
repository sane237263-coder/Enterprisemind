from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from enterprise_mind.identity.models import Base


class DeletionBatch(Base):
    __tablename__ = "deletion_batches"
    __table_args__ = (
        CheckConstraint(
            "root_type IN ('knowledge_base', 'folder', 'document')",
            name="ck_deletion_batches_root_type",
        ),
        CheckConstraint(
            "status IN ('active', 'purging', 'failed')", name="ck_deletion_batches_status"
        ),
        CheckConstraint("purge_after > deleted_at", name="ck_deletion_batches_retention"),
        {"schema": "knowledge"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    root_type: Mapped[str] = mapped_column(String(30))
    root_id: Mapped[int] = mapped_column(BigInteger)
    root_name: Mapped[str] = mapped_column(String(255))
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    failure_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        CheckConstraint(
            "(type = 'public' AND department_id IS NULL) OR "
            "(type = 'department' AND department_id IS NOT NULL)",
            name="ck_knowledge_bases_type_department",
        ),
        CheckConstraint("row_version >= 1", name="ck_knowledge_bases_row_version"),
        CheckConstraint("length(name_key) > 0", name="ck_knowledge_bases_name_key"),
        CheckConstraint(
            "(deleted_at IS NULL AND purge_after IS NULL AND deletion_batch_id IS NULL) OR "
            "(deleted_at IS NOT NULL AND purge_after IS NOT NULL "
            "AND purge_after > deleted_at AND deletion_batch_id IS NOT NULL)",
            name="ck_knowledge_bases_deletion",
        ),
        Index(
            "uq_knowledge_bases_public_name",
            "name_key",
            unique=True,
            postgresql_where=text("type = 'public' AND deleted_at IS NULL"),
        ),
        Index(
            "uq_knowledge_bases_department_name",
            "department_id",
            "name_key",
            unique=True,
            postgresql_where=text("type = 'department' AND deleted_at IS NULL"),
        ),
        Index("ix_knowledge_bases_department_id", "department_id"),
        {"schema": "knowledge"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(20))
    department_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("iam.departments.id", ondelete="RESTRICT")
    )
    description: Mapped[str | None] = mapped_column(Text)
    row_version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_batch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("knowledge.deletion_batches.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
