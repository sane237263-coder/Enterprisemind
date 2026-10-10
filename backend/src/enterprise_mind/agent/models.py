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
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from enterprise_mind.identity.models import Base


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_conversation_owner"),
        CheckConstraint(
            "next_message_sequence > 0 AND row_version > 0", name="ck_conversation_counts"
        ),
        Index("ix_conversations_owner_recent", "owner_user_id", "last_message_at", "id"),
        {"schema": "agent"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("iam.users.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(String(300))
    next_message_sequence: Mapped[int] = mapped_column(BigInteger, server_default="1")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(BigInteger, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_message_sequence"),
        UniqueConstraint("id", "conversation_id", name="uq_message_conversation"),
        CheckConstraint("role IN ('user','assistant')", name="ck_message_role"),
        CheckConstraint(
            "status IN ('streaming','completed','failed','stopped')", name="ck_message_status"
        ),
        CheckConstraint(
            "message_type IN ('text','mixed','status','error')", name="ck_message_type"
        ),
        CheckConstraint(
            "sequence > 0 AND (token_count IS NULL OR token_count >= 0)", name="ck_message_counts"
        ),
        {"schema": "agent"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent.conversations.id", ondelete="CASCADE")
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(20))
    message_type: Mapped[str] = mapped_column(String(30), server_default="text")
    content_text: Mapped[str | None] = mapped_column(Text)
    content_parts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20))
    token_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"
    __table_args__ = (
        UniqueConstraint("conversation_id", "through_sequence", name="uq_summary_sequence"),
        {"schema": "agent"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent.conversations.id", ondelete="CASCADE")
    )
    through_sequence: Mapped[int] = mapped_column(BigInteger)
    summary_text: Mapped[str] = mapped_column(Text)
    source_token_count: Mapped[int | None] = mapped_column(Integer)
    summary_token_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "actor_user_id"],
            ["agent.conversations.id", "agent.conversations.owner_user_id"],
            ondelete="CASCADE",
            name="fk_run_conversation_owner",
        ),
        ForeignKeyConstraint(
            ["user_message_id", "conversation_id"],
            ["agent.messages.id", "agent.messages.conversation_id"],
            ondelete="CASCADE",
            name="fk_run_user_message",
        ),
        ForeignKeyConstraint(
            ["assistant_message_id", "conversation_id"],
            ["agent.messages.id", "agent.messages.conversation_id"],
            ondelete="SET NULL (assistant_message_id)",
            name="fk_run_assistant_message",
        ),
        UniqueConstraint("user_message_id", name="uq_run_user_message"),
        UniqueConstraint("assistant_message_id", name="uq_run_assistant_message"),
        CheckConstraint(
            "status IN ('pending','running','waiting_confirmation',"
            "'cancelled','completed','failed')",
            name="ck_run_status",
        ),
        CheckConstraint(
            "tool_call_count >= 0 AND replan_count >= 0 AND retry_count >= 0", name="ck_run_counts"
        ),
        CheckConstraint(
            "status NOT IN ('completed','failed','cancelled') OR finished_at IS NOT NULL",
            name="ck_run_terminal_time",
        ),
        Index("ix_runs_recovery", "status", "updated_at", "id"),
        Index(
            "uq_conversation_active_run",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('pending','running','waiting_confirmation')"),
        ),
        {"schema": "agent"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(BigInteger)
    user_message_id: Mapped[int] = mapped_column(BigInteger)
    assistant_message_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("iam.users.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(30), server_default="pending")
    current_step: Mapped[str | None] = mapped_column(String(30))
    step_message: Mapped[str | None] = mapped_column(String(500))
    permission_scope_version: Mapped[str] = mapped_column(String(100))
    tool_call_count: Mapped[int] = mapped_column(Integer, server_default="0")
    replan_count: Mapped[int] = mapped_column(Integer, server_default="0")
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0")
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "idempotency_key", name="uq_tool_idempotency"),
        CheckConstraint(
            "status IN ('pending','waiting_confirmation','running','success','failed','cancelled')",
            name="ck_tool_status",
        ),
        CheckConstraint("risk_level IN ('low','high') AND retry_count >= 0", name="ck_tool_limits"),
        Index("ix_tool_run", "agent_run_id", "id"),
        {"schema": "agent"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent.agent_runs.id", ondelete="CASCADE")
    )
    actor_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("iam.users.id", ondelete="RESTRICT")
    )
    tool_name: Mapped[str] = mapped_column(String(200))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB)
    arguments_hash: Mapped[str] = mapped_column(String(64))
    permission_scope_version: Mapped[str] = mapped_column(String(100))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    risk_level: Mapped[str] = mapped_column(String(20), server_default="low")
    status: Mapped[str] = mapped_column(String(30), server_default="pending")
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0")
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnswerSource(Base):
    __tablename__ = "answer_sources"
    __table_args__ = (
        UniqueConstraint("message_id", "ordinal", name="uq_answer_source_ordinal"),
        Index("ix_answer_sources_database_source", "database_source_id", "schema_snapshot_id"),
        CheckConstraint(
            "source_type IN ('knowledge','database') AND ordinal > 0",
            name="ck_answer_source_type",
        ),
        {"schema": "agent"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent.messages.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    source_type: Mapped[str] = mapped_column(String(20), server_default="knowledge")
    chunk_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("knowledge.chunks.id", ondelete="SET NULL")
    )
    anchor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("knowledge.source_anchors.id", ondelete="SET NULL")
    )
    source_title: Mapped[str] = mapped_column(String(500))
    quote_text: Mapped[str | None] = mapped_column(Text)
    locator_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    relevance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    database_query_id: Mapped[str | None] = mapped_column(String(64))
    database_result_id: Mapped[str | None] = mapped_column(String(64))
    database_query_kind: Mapped[str | None] = mapped_column(String(20))
    database_row_count: Mapped[int | None] = mapped_column(Integer)
    database_tool_call_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent.tool_calls.id", ondelete="SET NULL")
    )
    database_source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("integration.database_sources.id", ondelete="SET NULL")
    )
    schema_snapshot_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("integration.schema_snapshots.id", ondelete="SET NULL")
    )
    referenced_relations: Mapped[list[str] | None] = mapped_column(JSONB)
    queried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    conditions_summary: Mapped[str | None] = mapped_column(Text)
    has_more: Mapped[bool | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
