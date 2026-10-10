from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from enterprise_mind.identity.models import Base

PROCESSING_STATES = "'pending','processing','success','partial_success','failed'"
STAGES = (
    "'parse','ocr','vision','table','code','chunk','embedding','keyword_index','preview','activate'"
)
DELETION_CHECK = (
    "(deleted_at IS NULL AND purge_after IS NULL AND deletion_batch_id IS NULL) OR "
    "(deleted_at IS NOT NULL AND purge_after IS NOT NULL AND deletion_batch_id IS NOT NULL "
    "AND purge_after > deleted_at)"
)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Folder(Timestamped, Base):
    """Folder identity with independent soft-deletion and parent scope."""

    __tablename__ = "folders"
    __table_args__ = (
        UniqueConstraint("id", "knowledge_base_id", name="uq_folders_id_base"),
        ForeignKeyConstraint(
            ["parent_id", "knowledge_base_id"],
            ["knowledge.folders.id", "knowledge.folders.knowledge_base_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "row_version >= 1 AND length(name_key) > 0", name="ck_folders_name_version"
        ),
        CheckConstraint(DELETION_CHECK, name="ck_folders_deletion"),
        Index(
            "uq_folders_root_name",
            "knowledge_base_id",
            "name_key",
            unique=True,
            postgresql_where=text("parent_id IS NULL AND deleted_at IS NULL"),
        ),
        Index(
            "uq_folders_child_name",
            "knowledge_base_id",
            "parent_id",
            "name_key",
            unique=True,
            postgresql_where=text("parent_id IS NOT NULL AND deleted_at IS NULL"),
        ),
        {"schema": "knowledge"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    knowledge_base_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge.knowledge_bases.id", ondelete="RESTRICT")
    )
    parent_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(255))
    name_key: Mapped[str] = mapped_column(String(255))
    row_version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_batch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("knowledge.deletion_batches.id", ondelete="RESTRICT")
    )


Index(
    "ix_folders_name_trgm",
    Folder.name_key,
    postgresql_using="gin",
    postgresql_ops={"name_key": "gin_trgm_ops"},
    postgresql_where=Folder.deleted_at.is_(None),
)


class Document(Timestamped, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("id", "knowledge_base_id", name="uq_documents_id_base"),
        ForeignKeyConstraint(
            ["folder_id", "knowledge_base_id"],
            ["knowledge.folders.id", "knowledge.folders.knowledge_base_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["active_revision_id", "id"],
            ["knowledge.document_revisions.id", "knowledge.document_revisions.document_id"],
            name="fk_documents_active_revision",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(f"processing_status IN ({PROCESSING_STATES})", name="ck_documents_status"),
        CheckConstraint(
            "row_version >= 1 AND latest_revision_no >= 1 AND length(name_key) > 0",
            name="ck_documents_name_version",
        ),
        CheckConstraint(DELETION_CHECK, name="ck_documents_deletion"),
        Index(
            "uq_documents_root_name",
            "knowledge_base_id",
            "name_key",
            unique=True,
            postgresql_where=text("folder_id IS NULL AND deleted_at IS NULL"),
        ),
        Index(
            "uq_documents_folder_name",
            "knowledge_base_id",
            "folder_id",
            "name_key",
            unique=True,
            postgresql_where=text("folder_id IS NOT NULL AND deleted_at IS NULL"),
        ),
        {"schema": "knowledge"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    knowledge_base_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge.knowledge_bases.id", ondelete="RESTRICT")
    )
    folder_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(255))
    name_key: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("iam.users.id", ondelete="RESTRICT")
    )
    processing_status: Mapped[str] = mapped_column(String(30), server_default="pending")
    latest_revision_no: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    active_revision_id: Mapped[int | None] = mapped_column(BigInteger)
    row_version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_batch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("knowledge.deletion_batches.id", ondelete="RESTRICT")
    )


Index(
    "ix_documents_name_trgm",
    Document.name_key,
    postgresql_using="gin",
    postgresql_ops={"name_key": "gin_trgm_ops"},
    postgresql_where=Document.deleted_at.is_(None),
)
Index(
    "ix_documents_description_trgm",
    Document.description,
    postgresql_using="gin",
    postgresql_ops={"description": "gin_trgm_ops"},
    postgresql_where=Document.deleted_at.is_(None) & Document.description.is_not(None),
)


class Revision(Timestamped, Base):
    __tablename__ = "document_revisions"
    __table_args__ = (
        UniqueConstraint("document_id", "revision_no", name="uq_revisions_document_no"),
        UniqueConstraint("id", "document_id", name="uq_revisions_id_document"),
        UniqueConstraint("original_object_id", name="uq_revisions_original_object"),
        CheckConstraint("revision_no >= 1 AND size_bytes >= 0", name="ck_revisions_values"),
        CheckConstraint(
            "trigger_type IN ('initial_upload','overwrite','generated_write')",
            name="ck_revisions_trigger",
        ),
        CheckConstraint(f"processing_status IN ({PROCESSING_STATES})", name="ck_revisions_status"),
        Index(
            "uq_revisions_unfinished",
            "document_id",
            unique=True,
            postgresql_where=text("processing_status IN ('pending','processing')"),
        ),
        {"schema": "knowledge"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge.documents.id", ondelete="CASCADE")
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    trigger_type: Mapped[str] = mapped_column(String(30))
    original_object_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("infra.storage_objects.id", ondelete="RESTRICT")
    )
    original_file_name: Mapped[str] = mapped_column(String(500))
    file_extension: Mapped[str] = mapped_column(String(20))
    mime_type: Mapped[str] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_sha256: Mapped[str] = mapped_column(CHAR(64))
    processing_status: Mapped[str] = mapped_column(String(30), server_default="pending")
    initiated_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("iam.users.id", ondelete="RESTRICT")
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ProcessingTask(Timestamped, Base):
    __tablename__ = "processing_tasks"
    __table_args__ = (
        UniqueConstraint("id", "revision_id", name="uq_tasks_id_revision"),
        CheckConstraint(
            "task_type IN ('full','retry_failed','preview_rebuild')", name="ck_tasks_type"
        ),
        CheckConstraint(f"status IN ({PROCESSING_STATES})", name="ck_tasks_status"),
        CheckConstraint(
            f"current_stage IS NULL OR current_stage IN ({STAGES})", name="ck_tasks_stage"
        ),
        CheckConstraint(
            "(stage_current IS NULL AND stage_total IS NULL) OR "
            "(stage_current IS NOT NULL AND stage_total IS NOT NULL "
            "AND stage_current >= 0 AND stage_total >= stage_current)",
            name="ck_tasks_progress",
        ),
        Index(
            "uq_tasks_unfinished",
            "revision_id",
            unique=True,
            postgresql_where=text("status IN ('pending','processing')"),
        ),
        {"schema": "knowledge"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge.document_revisions.id", ondelete="CASCADE")
    )
    task_type: Mapped[str] = mapped_column(String(20))
    retry_of_task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("knowledge.processing_tasks.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(30), server_default="pending")
    current_stage: Mapped[str | None] = mapped_column(String(30))
    stage_current: Mapped[int | None] = mapped_column(Integer)
    stage_total: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceUnit(Timestamped, Base):
    __tablename__ = "source_units"
    __table_args__ = (
        UniqueConstraint("revision_id", "unit_type", "ordinal", name="uq_source_units_order"),
        UniqueConstraint("id", "revision_id", name="uq_source_units_id_revision"),
        CheckConstraint(
            "unit_type IN ('document','page','slide','sheet','image')", name="ck_source_units_type"
        ),
        CheckConstraint("ordinal >= 0", name="ck_source_units_ordinal"),
        CheckConstraint(
            "(width IS NULL AND height IS NULL) OR "
            "(width IS NOT NULL AND height IS NOT NULL AND width > 0 AND height > 0)",
            name="ck_source_units_size",
        ),
        CheckConstraint(
            "extraction_status IN ('pending','success','failed','skipped')",
            name="ck_source_units_status",
        ),
        {"schema": "knowledge"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge.document_revisions.id", ondelete="CASCADE")
    )
    unit_type: Mapped[str] = mapped_column(String(20))
    ordinal: Mapped[int] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(255))
    width: Mapped[Decimal | None] = mapped_column(Numeric)
    height: Mapped[Decimal | None] = mapped_column(Numeric)
    extraction_status: Mapped[str] = mapped_column(String(20))
    extracted_text: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)


class ProcessingUnit(Base):
    __tablename__ = "processing_units"
    __table_args__ = (
        UniqueConstraint("revision_id", "stage", "unit_key", name="uq_processing_units_key"),
        ForeignKeyConstraint(
            ["latest_task_id", "revision_id"],
            ["knowledge.processing_tasks.id", "knowledge.processing_tasks.revision_id"],
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["source_unit_id", "revision_id"],
            ["knowledge.source_units.id", "knowledge.source_units.revision_id"],
            ondelete="SET NULL (source_unit_id)",
        ),
        CheckConstraint(f"stage IN ({STAGES})", name="ck_processing_units_stage"),
        CheckConstraint(
            "status IN ('pending','processing','success','failed','skipped')",
            name="ck_processing_units_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_processing_units_attempts"),
        CheckConstraint(
            "(current IS NULL AND total IS NULL) OR "
            "(current IS NOT NULL AND total IS NOT NULL AND current >= 0 AND total >= current)",
            name="ck_processing_units_progress",
        ),
        CheckConstraint(
            "(status = 'processing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR "
            "(status <> 'processing' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_processing_units_lease",
        ),
        Index("ix_processing_units_pending", "status", "stage", "lease_expires_at"),
        {"schema": "knowledge"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge.document_revisions.id", ondelete="CASCADE")
    )
    stage: Mapped[str] = mapped_column(String(30))
    unit_key: Mapped[str] = mapped_column(String(200))
    source_unit_id: Mapped[int | None] = mapped_column(BigInteger)
    latest_task_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    current: Mapped[int | None] = mapped_column(Integer)
    total: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContentElement(Base):
    __tablename__ = "content_elements"
    __table_args__ = (
        UniqueConstraint("source_unit_id", "ordinal", name="uq_elements_order"),
        UniqueConstraint("id", "source_unit_id", name="uq_elements_id_source"),
        ForeignKeyConstraint(
            ["parent_element_id", "source_unit_id"],
            ["knowledge.content_elements.id", "knowledge.content_elements.source_unit_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "ordinal >= 0 AND (token_estimate IS NULL OR token_estimate >= 0)",
            name="ck_elements_values",
        ),
        CheckConstraint("status IN ('success','failed')", name="ck_elements_status"),
        {"schema": "knowledge"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source_unit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge.source_units.id", ondelete="CASCADE")
    )
    parent_element_id: Mapped[int | None] = mapped_column(BigInteger)
    element_type: Mapped[str] = mapped_column(String(30))
    ordinal: Mapped[int] = mapped_column(Integer)
    content_text: Mapped[str | None] = mapped_column(Text)
    structured_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    token_estimate: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), server_default="success")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceAnchor(Base):
    __tablename__ = "source_anchors"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_unit_id", "revision_id"],
            ["knowledge.source_units.id", "knowledge.source_units.revision_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["element_id", "source_unit_id"],
            ["knowledge.content_elements.id", "knowledge.content_elements.source_unit_id"],
            ondelete="SET NULL (element_id)",
        ),
        CheckConstraint(
            "locator_type IN ('pdf','image','docx','pptx','xlsx','text')", name="ck_anchors_type"
        ),
        {"schema": "knowledge"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge.document_revisions.id", ondelete="CASCADE")
    )
    source_unit_id: Mapped[int] = mapped_column(BigInteger)
    element_id: Mapped[int | None] = mapped_column(BigInteger)
    locator_type: Mapped[str] = mapped_column(String(20))
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_text: Mapped[str | None] = mapped_column(Text)
    source_text_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index("ix_source_anchors_source", SourceAnchor.source_unit_id, SourceAnchor.id)


class DocumentArtifact(Timestamped, Base):
    __tablename__ = "document_artifacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_unit_id", "revision_id"],
            ["knowledge.source_units.id", "knowledge.source_units.revision_id"],
            ondelete="CASCADE",
            name="fk_artifacts_source",
        ),
        UniqueConstraint(
            "revision_id",
            "source_unit_id",
            "artifact_type",
            name="uq_artifacts_output",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("status IN ('pending','ready','failed')", name="ck_artifacts_status"),
        Index("ix_artifacts_revision", "revision_id"),
        {"schema": "knowledge"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge.document_revisions.id", ondelete="CASCADE")
    )
    source_unit_id: Mapped[int | None] = mapped_column(BigInteger)
    artifact_type: Mapped[str] = mapped_column(String(40))
    storage_object_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("infra.storage_objects.id", ondelete="RESTRICT"), unique=True
    )
    status: Mapped[str] = mapped_column(String(20), server_default="ready")
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)


class StorageObject(Timestamped, Base):
    __tablename__ = "storage_objects"
    __table_args__ = (
        UniqueConstraint("bucket_name", "object_key", name="uq_storage_objects_key"),
        CheckConstraint(
            "size_bytes >= 0 AND length(bucket_name) > 0 AND length(object_key) > 0",
            name="ck_storage_objects_values",
        ),
        CheckConstraint(
            "object_type IN ('original','preview','derived','generated','export')",
            name="ck_storage_objects_type",
        ),
        CheckConstraint(
            "status IN ('staged','available','pending_delete','deleting','deleted','error')",
            name="ck_storage_objects_status",
        ),
        CheckConstraint(
            "(attachment_file_name IS NULL AND attachment_file_extension IS NULL "
            "AND attachment_conversation_id IS NULL) OR (attachment_file_name IS NOT NULL "
            "AND length(attachment_file_name) > 0 AND attachment_file_extension IS NOT NULL "
            "AND length(attachment_file_extension) > 0 AND object_type = 'original')",
            name="ck_storage_attachment_metadata",
        ),
        Index(
            "ix_storage_attachment_conversation",
            "attachment_conversation_id",
            "id",
            postgresql_where=text("attachment_conversation_id IS NOT NULL"),
        ),
        {"schema": "infra"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bucket_name: Mapped[str] = mapped_column(String(100))
    object_key: Mapped[str] = mapped_column(String(1000))
    object_type: Mapped[str] = mapped_column(String(30))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(200))
    content_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    status: Mapped[str] = mapped_column(String(20))
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    attachment_conversation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "agent.conversations.id", ondelete="SET NULL", name="fk_storage_attachment_conversation"
        ),
    )
    attachment_file_name: Mapped[str | None] = mapped_column(String(500))
    attachment_file_extension: Mapped[str | None] = mapped_column(String(20))


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("deduplication_key", name="uq_outbox_deduplication"),
        CheckConstraint(
            "status IN ('pending','publishing','published','failed')", name="ck_outbox_status"
        ),
        CheckConstraint(
            "attempt_count >= 0 AND (status <> 'published' OR published_at IS NOT NULL)",
            name="ck_outbox_values",
        ),
        Index("ix_outbox_dispatch", "status", "next_attempt_at"),
        {"schema": "infra"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(50))
    aggregate_id: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    deduplication_key: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
