from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from enterprise_mind.identity.models import Base


class IdempotencyRecord(Base):
    """A committed creation result, never a request log or a credential store."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "idempotency_key", name="uq_idempotency_actor_key"),
        {"schema": "infra"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    actor_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("iam.users.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(200))
    operation_name: Mapped[str] = mapped_column(String(100))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[int] = mapped_column(BigInteger)
    response_data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
