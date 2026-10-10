"""Allow preview-only tasks without changing document ingestion state."""

from alembic import op

revision = "0009_preview_rebuild"
down_revision = "0008_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE knowledge.processing_tasks DROP CONSTRAINT ck_tasks_type")
    op.execute("""
ALTER TABLE knowledge.processing_tasks ADD CONSTRAINT ck_tasks_type
CHECK (task_type IN ('full','retry_failed','preview_rebuild'))
""")


def downgrade() -> None:
    raise RuntimeError("Rollback requires an explicit data recovery plan.")
