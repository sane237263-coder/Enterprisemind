from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    desc,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from enterprise_mind.identity.models import Base


class MemoryState(Base):
    __tablename__ = "user_memory_states"
    __table_args__ = (
        CheckConstraint("memory_epoch >= 0", name="ck_user_memory_states_epoch"),
        {"schema": "agent"},
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("iam.users.id", ondelete="CASCADE"), primary_key=True
    )
    memory_epoch: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Memory(Base):
    __tablename__ = "personal_memories"
    __table_args__ = (
        CheckConstraint(
            "category IN ('preference','work_context','ongoing_context','other')",
            name="ck_personal_memories_category",
        ),
        CheckConstraint(
            "origin IN ('explicit','manual','automatic')", name="ck_personal_memories_origin"
        ),
        CheckConstraint(
            "length(trim(content)) > 0 AND char_length(content) <= 1000",
            name="ck_personal_memories_content",
        ),
        CheckConstraint("revision >= 1", name="ck_personal_memories_revision"),
        Index(
            "uq_personal_memories_subject",
            "user_id",
            "subject_key",
            unique=True,
            postgresql_where=text("subject_key IS NOT NULL"),
        ),
        Index(
            "ix_personal_memories_user_updated",
            "user_id",
            desc("updated_at"),
            desc("id"),
        ),
        Index(
            "ix_personal_memories_user_category",
            "user_id",
            "category",
            desc("updated_at"),
            desc("id"),
        ),
        {"schema": "agent"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("iam.users.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(30))
    subject_key: Mapped[str | None] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(CHAR(64))
    origin: Mapped[str] = mapped_column(String(20))
    source_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent.messages.id", ondelete="SET NULL")
    )
    revision: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


__all__ = ["Memory", "MemoryState"]
