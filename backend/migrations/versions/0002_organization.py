"""Organization management and durable creation idempotency."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_organization"
down_revision = "0001_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deletion_batches",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("root_type", sa.String(30), nullable=False),
        sa.Column("root_id", sa.BigInteger(), nullable=False),
        sa.Column("root_name", sa.String(255), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("failure_summary", postgresql.JSONB()),
        sa.CheckConstraint(
            "root_type IN ('knowledge_base', 'folder', 'document')",
            name="ck_deletion_batches_root_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'purging', 'failed')", name="ck_deletion_batches_status"
        ),
        sa.CheckConstraint("purge_after > deleted_at", name="ck_deletion_batches_retention"),
        schema="knowledge",
    )
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("name_key", sa.String(200), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("department_id", sa.BigInteger()),
        sa.Column("description", sa.Text()),
        sa.Column("row_version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("purge_after", sa.DateTime(timezone=True)),
        sa.Column("deletion_batch_id", sa.BigInteger()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["department_id"], ["iam.departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["deletion_batch_id"], ["knowledge.deletion_batches.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "(type = 'public' AND department_id IS NULL) OR "
            "(type = 'department' AND department_id IS NOT NULL)",
            name="ck_knowledge_bases_type_department",
        ),
        sa.CheckConstraint("row_version >= 1", name="ck_knowledge_bases_row_version"),
        sa.CheckConstraint("length(name_key) > 0", name="ck_knowledge_bases_name_key"),
        sa.CheckConstraint(
            "(deleted_at IS NULL AND purge_after IS NULL AND deletion_batch_id IS NULL) OR "
            "(deleted_at IS NOT NULL AND purge_after IS NOT NULL "
            "AND purge_after > deleted_at AND deletion_batch_id IS NOT NULL)",
            name="ck_knowledge_bases_deletion",
        ),
        schema="knowledge",
    )
    op.create_index(
        "uq_knowledge_bases_public_name",
        "knowledge_bases",
        ["name_key"],
        schema="knowledge",
        unique=True,
        postgresql_where=sa.text("type = 'public' AND deleted_at IS NULL"),
    )
    op.create_index(
        "uq_knowledge_bases_department_name",
        "knowledge_bases",
        ["department_id", "name_key"],
        schema="knowledge",
        unique=True,
        postgresql_where=sa.text("type = 'department' AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_knowledge_bases_department_id", "knowledge_bases", ["department_id"], schema="knowledge"
    )
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("operation_name", sa.String(100), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.BigInteger(), nullable=False),
        sa.Column("response_data", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["iam.users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("actor_user_id", "idempotency_key", name="uq_idempotency_actor_key"),
        schema="infra",
    )


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; automatic destructive downgrade is disabled.")
