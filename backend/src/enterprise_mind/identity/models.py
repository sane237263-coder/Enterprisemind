from datetime import datetime

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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_departments_status"),
        CheckConstraint("row_version >= 1", name="ck_departments_row_version"),
        CheckConstraint("length(name_key) > 0", name="ck_departments_name_key"),
        {"schema": "iam"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    row_version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'user')", name="ck_users_role"),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_users_status"),
        CheckConstraint("role <> 'user' OR department_id IS NOT NULL", name="ck_users_department"),
        CheckConstraint("credential_version >= 1", name="ck_users_credential_version"),
        CheckConstraint("row_version >= 1", name="ck_users_row_version"),
        CheckConstraint("length(username_key) > 0", name="ck_users_username_key"),
        Index("ix_users_department_id", "department_id"),
        {"schema": "iam"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    username: Mapped[str] = mapped_column(String(100))
    username_key: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    credential_version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    role: Mapped[str] = mapped_column(String(20))
    department_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("iam.departments.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    row_version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
