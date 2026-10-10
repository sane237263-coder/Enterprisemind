"""Stage 1: namespaces, extensions and local identities."""

import sqlalchemy as sa
from alembic import op

revision = "0001_identity"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # infra is created by the locked migration runner for Alembic's version table.
    for schema in ("iam", "knowledge", "agent", "integration", "langgraph_checkpoint"):
        op.execute(sa.schema.CreateSchema(schema))
    op.execute("CREATE EXTENSION IF NOT EXISTS vector VERSION '0.8.6'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "departments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("name_key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("row_version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_departments_status"),
        sa.CheckConstraint("row_version >= 1", name="ck_departments_row_version"),
        sa.CheckConstraint("length(name_key) > 0", name="ck_departments_name_key"),
        sa.UniqueConstraint("name_key", name="uq_departments_name_key"),
        schema="iam",
    )
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("username_key", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "credential_version", sa.BigInteger(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("department_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("row_version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("role IN ('admin', 'user')", name="ck_users_role"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_users_status"),
        sa.CheckConstraint(
            "role <> 'user' OR department_id IS NOT NULL", name="ck_users_department"
        ),
        sa.CheckConstraint("credential_version >= 1", name="ck_users_credential_version"),
        sa.CheckConstraint("row_version >= 1", name="ck_users_row_version"),
        sa.CheckConstraint("length(username_key) > 0", name="ck_users_username_key"),
        sa.UniqueConstraint("username_key", name="uq_users_username_key"),
        sa.ForeignKeyConstraint(["department_id"], ["iam.departments.id"], ondelete="RESTRICT"),
        schema="iam",
    )
    op.create_index("ix_users_department_id", "users", ["department_id"], schema="iam")


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; automatic destructive downgrade is disabled.")
