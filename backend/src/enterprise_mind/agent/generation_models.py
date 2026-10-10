from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from enterprise_mind.identity.models import Base


class ContentDraft(Base):
    __tablename__ = "content_drafts"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_content_drafts_revision"),
        CheckConstraint("status IN ('draft','ready')", name="ck_content_drafts_status"),
        CheckConstraint("length(body_markdown) > 0", name="ck_content_drafts_body"),
        Index(
            "ix_content_drafts_owner_updated",
            "owner_user_id",
            desc("updated_at"),
            desc("id"),
        ),
        {"schema": "agent"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("iam.users.id", ondelete="RESTRICT")
    )
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent.conversations.id", ondelete="SET NULL")
    )
    source_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent.messages.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(300))
    body_markdown: Mapped[str] = mapped_column(Text)
    tables: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    revision: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    content_hash: Mapped[str] = mapped_column(CHAR(64))
    status: Mapped[str] = mapped_column(String(20), server_default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GenerationTask(Base):
    __tablename__ = "generation_tasks"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "idempotency_key", name="uq_generation_task_idempotency"),
        CheckConstraint(
            "kind IN ('content_render','database_export','chart_render')",
            name="ck_generation_tasks_kind",
        ),
        CheckConstraint(
            "format IN ('docx','pdf','md','xlsx','png','csv')",
            name="ck_generation_tasks_format",
        ),
        CheckConstraint(
            "scope IS NULL OR scope IN ('current_page','all_matching','first_n','top_n')",
            name="ck_generation_tasks_scope",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_generation_tasks_status",
        ),
        CheckConstraint(
            "processed_rows >= 0 AND (total_rows IS NULL OR total_rows >= 0) "
            "AND (exported_rows IS NULL OR exported_rows >= 0)",
            name="ck_generation_tasks_rows",
        ),
        CheckConstraint(
            "execution_attempt_count >= 0",
            name="ck_generation_tasks_execution_attempt_count",
        ),
        Index(
            "ix_generation_tasks_owner_created",
            "owner_user_id",
            desc("created_at"),
            desc("id"),
        ),
        Index("ix_generation_tasks_recovery", "status", "updated_at", "id"),
        {"schema": "agent"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("iam.users.id", ondelete="RESTRICT")
    )
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent.conversations.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(30))
    format: Mapped[str] = mapped_column(String(20))
    scope: Mapped[str | None] = mapped_column(String(20))
    draft_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent.content_drafts.id", ondelete="SET NULL")
    )
    source_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent.messages.id", ondelete="SET NULL")
    )
    export_plan_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent.database_export_plans.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64))
    input_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(20), server_default="queued")
    phase: Mapped[str | None] = mapped_column(String(40))
    execution_owner: Mapped[str | None] = mapped_column(String(64))
    execution_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    processed_rows: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    total_rows: Mapped[int | None] = mapped_column(BigInteger)
    exported_rows: Mapped[int | None] = mapped_column(BigInteger)
    file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("infra.storage_objects.id", ondelete="SET NULL")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GeneratedFile(Base):
    __tablename__ = "generated_files"
    __table_args__ = (
        # A chart task publishes one file per requested format from one frozen
        # snapshot.  Legacy content/database tasks still have one row; the
        # format component prevents duplicate publication of one artifact.
        UniqueConstraint("task_id", "file_format", name="uq_generated_files_task_format"),
        UniqueConstraint("storage_object_id", name="uq_generated_files_object"),
        CheckConstraint(
            "file_format IN ('docx','pdf','md','xlsx','png','csv')",
            name="ck_generated_files_format",
        ),
        CheckConstraint(
            "generation_kind IN ('content_render','database_export','chart_render')",
            name="ck_generated_files_kind",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_generated_files_size"),
        Index(
            "ix_generated_files_owner_created",
            "owner_user_id",
            desc("generated_at"),
            desc("id"),
        ),
        {"schema": "agent"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent.generation_tasks.id", ondelete="CASCADE")
    )
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("iam.users.id", ondelete="RESTRICT")
    )
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent.conversations.id", ondelete="SET NULL")
    )
    storage_object_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("infra.storage_objects.id", ondelete="RESTRICT")
    )
    file_name: Mapped[str] = mapped_column(String(500))
    file_format: Mapped[str] = mapped_column(String(20))
    generation_kind: Mapped[str] = mapped_column(String(30))
    draft_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent.content_drafts.id", ondelete="SET NULL")
    )
    source_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent.messages.id", ondelete="SET NULL")
    )
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    result_scope: Mapped[str | None] = mapped_column(String(20))
    exported_rows: Mapped[int | None] = mapped_column(BigInteger)
    content_sha256: Mapped[str] = mapped_column(CHAR(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


__all__ = ["ContentDraft", "GeneratedFile", "GenerationTask"]
