"""Durable state for the single Database MCP source and schema snapshots.

The rows in this module describe an external PostgreSQL database.  They are
intentionally separate from the EnterpriseMind database connection and never
contain a password or a resolved Secret value.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from enterprise_mind.identity.models import Base


class DatabaseSource(Base):
    """The one external PostgreSQL source allowed by V1."""

    __tablename__ = "database_sources"
    __table_args__ = (
        UniqueConstraint("name", name="uq_database_sources_name"),
        CheckConstraint(
            "status IN ('active', 'disabled', 'error')", name="ck_database_sources_status"
        ),
        CheckConstraint("port BETWEEN 1 AND 65535", name="ck_database_sources_port"),
        CheckConstraint("row_version >= 1", name="ck_database_sources_row_version"),
        CheckConstraint(
            "configuration_version >= 1", name="ck_database_sources_configuration_version"
        ),
        CheckConstraint("length(name) > 0 AND length(host) > 0", name="ck_database_sources_names"),
        CheckConstraint("length(database_name) > 0", name="ck_database_sources_database_name"),
        CheckConstraint("length(secret_ref) > 0", name="ck_database_sources_secret_ref"),
        ForeignKeyConstraint(
            ["active_snapshot_id", "id"],
            ["integration.schema_snapshots.id", "integration.schema_snapshots.database_source_id"],
            ondelete="SET NULL (active_snapshot_id)",
            use_alter=True,
            name="fk_database_sources_active_snapshot",
        ),
        Index("uq_database_sources_singleton", text("(true)"), unique=True),
        Index("ix_database_sources_active_snapshot", "active_snapshot_id"),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    database_name: Mapped[str] = mapped_column(String(200))
    ssl_mode: Mapped[str] = mapped_column(String(30))
    secret_ref: Mapped[str] = mapped_column(String(500))
    connection_options: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    active_snapshot_id: Mapped[int | None] = mapped_column(BigInteger)
    row_version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    # Display-name changes use only row_version.  Status and
    # connection/credential/target changes increment this separate runtime
    # epoch so old query/page contexts cannot survive a disable -> enable
    # transition or a target change.
    configuration_version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SchemaSnapshot(Base):
    """A complete, independently built external schema snapshot."""

    __tablename__ = "schema_snapshots"
    __table_args__ = (
        UniqueConstraint("database_source_id", "snapshot_no", name="uq_schema_snapshots_source_no"),
        # Required by database_sources' composite active_snapshot_id FK.
        UniqueConstraint("id", "database_source_id", name="uq_schema_snapshots_id_source"),
        CheckConstraint("snapshot_no >= 1", name="ck_schema_snapshots_no"),
        CheckConstraint(
            "status IN ('building', 'ready', 'active', 'failed', 'retired')",
            name="ck_schema_snapshots_status",
        ),
        CheckConstraint(
            "status NOT IN ('ready', 'active') OR captured_at IS NOT NULL",
            name="ck_schema_snapshots_captured",
        ),
        ForeignKeyConstraint(
            ["database_source_id"],
            ["integration.database_sources.id"],
            ondelete="RESTRICT",
            name="fk_schema_snapshots_source",
        ),
        Index("ix_schema_snapshots_source_status", "database_source_id", "status"),
        Index(
            "uq_schema_snapshots_active",
            "database_source_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    database_source_id: Mapped[int] = mapped_column(BigInteger)
    snapshot_no: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), server_default="building")
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SchemaRelation(Base):
    __tablename__ = "schema_relations"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "schema_name", "relation_name", name="uq_schema_relations_name"
        ),
        CheckConstraint("relation_type IN ('table', 'view')", name="ck_schema_relations_type"),
        CheckConstraint(
            "relation_oid IS NULL OR relation_oid >= 0", name="ck_schema_relations_oid"
        ),
        ForeignKeyConstraint(
            ["snapshot_id"],
            ["integration.schema_snapshots.id"],
            ondelete="CASCADE",
            name="fk_schema_relations_snapshot",
        ),
        Index("ix_schema_relations_snapshot", "snapshot_id", "id"),
        Index(
            "uq_schema_relations_snapshot_oid",
            "snapshot_id",
            "relation_oid",
            unique=True,
            postgresql_where=text("relation_oid IS NOT NULL"),
        ),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(BigInteger)
    schema_name: Mapped[str] = mapped_column(String(200))
    relation_name: Mapped[str] = mapped_column(String(200))
    relation_type: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str | None] = mapped_column(Text)
    # The external PostgreSQL OID and signatures are internal continuity
    # guards; they are never projected to the model or ordinary users.
    relation_oid: Mapped[int | None] = mapped_column(BigInteger)
    relation_signature: Mapped[str | None] = mapped_column(String(128))
    view_definition_signature: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SchemaColumn(Base):
    __tablename__ = "schema_columns"
    __table_args__ = (
        UniqueConstraint("relation_id", "column_name", name="uq_schema_columns_name"),
        UniqueConstraint("relation_id", "ordinal", name="uq_schema_columns_ordinal"),
        CheckConstraint("ordinal >= 1", name="ck_schema_columns_ordinal"),
        ForeignKeyConstraint(
            ["relation_id"],
            ["integration.schema_relations.id"],
            ondelete="CASCADE",
            name="fk_schema_columns_relation",
        ),
        Index("ix_schema_columns_relation", "relation_id", "ordinal"),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    relation_id: Mapped[int] = mapped_column(BigInteger)
    ordinal: Mapped[int] = mapped_column(Integer)
    column_name: Mapped[str] = mapped_column(String(200))
    data_type: Mapped[str] = mapped_column(String(300))
    comment: Mapped[str | None] = mapped_column(Text)
    is_nullable: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SchemaConstraint(Base):
    __tablename__ = "schema_constraints"
    __table_args__ = (
        UniqueConstraint("relation_id", "constraint_name", name="uq_schema_constraints_name"),
        CheckConstraint(
            "constraint_type IN ('primary_key', 'unique', 'foreign_key')",
            name="ck_schema_constraints_type",
        ),
        CheckConstraint("cardinality(column_names) > 0", name="ck_schema_constraints_columns"),
        CheckConstraint(
            "(constraint_type IN ('primary_key', 'unique') AND referenced_schema_name IS NULL "
            "AND referenced_relation_name IS NULL AND referenced_column_names IS NULL) OR "
            "(constraint_type = 'foreign_key' AND referenced_schema_name IS NOT NULL "
            "AND referenced_relation_name IS NOT NULL AND referenced_column_names IS NOT NULL "
            "AND cardinality(referenced_column_names) = cardinality(column_names))",
            name="ck_schema_constraints_reference",
        ),
        ForeignKeyConstraint(
            ["relation_id"],
            ["integration.schema_relations.id"],
            ondelete="CASCADE",
            name="fk_schema_constraints_relation",
        ),
        Index("ix_schema_constraints_relation", "relation_id"),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    relation_id: Mapped[int] = mapped_column(BigInteger)
    constraint_name: Mapped[str] = mapped_column(String(300))
    constraint_type: Mapped[str] = mapped_column(String(20))
    column_names: Mapped[list[str]] = mapped_column(ARRAY(Text))
    referenced_schema_name: Mapped[str | None] = mapped_column(String(200))
    referenced_relation_name: Mapped[str | None] = mapped_column(String(200))
    referenced_column_names: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RelationPolicy(Base):
    """A source-wide allowlist entry preserved only for the same object shape."""

    __tablename__ = "relation_policies"
    __table_args__ = (
        UniqueConstraint(
            "database_source_id",
            "schema_name",
            "relation_name",
            name="uq_relation_policies_name",
        ),
        CheckConstraint("row_version >= 1", name="ck_relation_policies_row_version"),
        ForeignKeyConstraint(
            ["database_source_id"],
            ["integration.database_sources.id"],
            ondelete="RESTRICT",
            name="fk_relation_policies_source",
        ),
        Index("ix_relation_policies_source_enabled", "database_source_id", "enabled"),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    database_source_id: Mapped[int] = mapped_column(BigInteger)
    schema_name: Mapped[str] = mapped_column(String(200))
    relation_name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    row_version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RelationDepartmentGrant(Base):
    __tablename__ = "relation_department_grants"
    __table_args__ = (
        UniqueConstraint(
            "relation_policy_id", "department_id", name="uq_relation_department_grants"
        ),
        ForeignKeyConstraint(
            ["relation_policy_id"],
            ["integration.relation_policies.id"],
            ondelete="CASCADE",
            name="fk_relation_department_grants_policy",
        ),
        ForeignKeyConstraint(
            ["department_id"],
            ["iam.departments.id"],
            ondelete="CASCADE",
            name="fk_relation_department_grants_department",
        ),
        Index("ix_relation_department_grants_department", "department_id", "relation_policy_id"),
        {"schema": "integration"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    relation_policy_id: Mapped[int] = mapped_column(BigInteger)
    department_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


__all__ = [
    "DatabaseSource",
    "RelationDepartmentGrant",
    "RelationPolicy",
    "SchemaColumn",
    "SchemaConstraint",
    "SchemaRelation",
    "SchemaSnapshot",
]
