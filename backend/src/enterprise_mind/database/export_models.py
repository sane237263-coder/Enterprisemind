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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from enterprise_mind.identity.models import Base


class DatabaseExportPlan(Base):
    """Short-lived, server-owned input for a tabular database export.

    SQL, bound values, and result rows are deliberately kept out of the
    database row, ordinary DTOs, model checkpoints, and logs.  ``runtime_handle``
    points to a bounded process-local plan; losing that process-local value is a
    safe execution failure rather than permission to reconstruct a query.
    """

    __tablename__ = "database_export_plans"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('current_page','all_matching','first_n')",
            name="ck_database_export_plans_scope",
        ),
        CheckConstraint("format IN ('xlsx','csv')", name="ck_database_export_plans_format"),
        CheckConstraint("source_version >= 1", name="ck_database_export_plans_version"),
        Index("ix_database_export_plans_expiry", "owner_user_id", "expires_at"),
        {"schema": "agent"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("iam.users.id", ondelete="RESTRICT")
    )
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent.conversations.id", ondelete="CASCADE")
    )
    query_id: Mapped[str | None] = mapped_column(String(64))
    result_id: Mapped[str | None] = mapped_column(String(64))
    runtime_handle: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(20))
    format: Mapped[str] = mapped_column(String(20), server_default="xlsx")
    database_source_id: Mapped[int] = mapped_column(BigInteger)
    schema_snapshot_id: Mapped[int] = mapped_column(BigInteger)
    source_version: Mapped[int] = mapped_column(BigInteger)
    permission_fingerprint: Mapped[str] = mapped_column(String(128))
    catalog: Mapped[dict[str, Any]] = mapped_column(JSONB)
    referenced_relations: Mapped[list[str]] = mapped_column(JSONB)
    conditions_summary: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


__all__ = ["DatabaseExportPlan"]
