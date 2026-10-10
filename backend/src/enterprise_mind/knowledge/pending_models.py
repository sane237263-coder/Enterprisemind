"""Durable confirmation state shared by HTTP and the Tool Gateway."""

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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from enterprise_mind.identity.models import Base


class PendingAction(Base):
    __tablename__ = "pending_actions"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "idempotency_key", name="uq_pending_actor_key"),
        CheckConstraint("action_type IN ('confirmation','disambiguation')", name="ck_pending_type"),
        CheckConstraint(
            "status IN ('pending','accepted','rejected','expired','cancelled')",
            name="ck_pending_status",
        ),
        CheckConstraint(
            "(origin = 'api' AND agent_run_id IS NULL AND tool_call_id IS NULL "
            "AND action_type = 'confirmation') OR (origin = 'agent' AND agent_run_id "
            "IS NOT NULL AND (action_type = 'disambiguation' OR tool_call_id IS NOT NULL))",
            name="ck_pending_origin",
        ),
        CheckConstraint(
            "expires_at > created_at AND (status = 'pending' OR resolved_at IS NOT NULL)",
            name="ck_pending_times",
        ),
        Index(
            "uq_pending_run",
            "agent_run_id",
            unique=True,
            postgresql_where=text("status = 'pending' AND agent_run_id IS NOT NULL"),
        ),
        Index("ix_pending_expiry", "status", "expires_at"),
        {"schema": "agent"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    origin: Mapped[str] = mapped_column(String(20))
    operation_name: Mapped[str] = mapped_column(String(200))
    agent_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent.agent_runs.id", ondelete="CASCADE")
    )
    tool_call_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent.tool_calls.id", ondelete="CASCADE")
    )
    actor_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("iam.users.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(200))
    action_type: Mapped[str] = mapped_column(String(30))
    prompt_text: Mapped[str] = mapped_column(Text)
    bound_arguments: Mapped[dict[str, Any]] = mapped_column(JSONB)
    binding_hash: Mapped[str] = mapped_column(CHAR(64))
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PendingActionTarget(Base):
    __tablename__ = "pending_action_targets"
    __table_args__ = (
        UniqueConstraint("pending_action_id", "ordinal", name="uq_pending_target_ordinal"),
        CheckConstraint(
            "ordinal >= 0 AND (expected_row_version IS NULL OR expected_row_version > 0)",
            name="ck_pending_target_values",
        ),
        {"schema": "agent"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    pending_action_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent.pending_actions.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    resource_type: Mapped[str] = mapped_column(String(30))
    resource_id: Mapped[int] = mapped_column(BigInteger)
    display_name: Mapped[str] = mapped_column(String(500))
    expected_row_version: Mapped[int | None] = mapped_column(BigInteger)
    target_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
