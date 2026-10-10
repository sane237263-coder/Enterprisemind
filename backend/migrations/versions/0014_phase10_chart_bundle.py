"""Publish all chart formats from one frozen generation task.

The Phase 10 chart worker renders the four bounded formats from one streamed
snapshot.  This migration removes the Phase 9 one-file-per-task uniqueness
rule and replaces it with one row per task and format.  Downgrade refuses to
hide a multi-format task; operators must first roll back code compatibly or
retain the newer schema.
"""

from alembic import op
from sqlalchemy import text

revision = "0014_phase10_chart_bundle"
down_revision = "0013_phase10_charts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_generated_files_task", "generated_files", schema="agent")
    op.create_unique_constraint(
        "uq_generated_files_task_format",
        "generated_files",
        ["task_id", "file_format"],
        schema="agent",
    )


def downgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(
        text(
            """
            SELECT 1
            FROM agent.generated_files
            GROUP BY task_id
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "refusing 0014 downgrade while a generation task has multiple artifacts; "
            "rollback the code first or retain the bundle schema"
        )
    op.drop_constraint("uq_generated_files_task_format", "generated_files", schema="agent")
    op.create_unique_constraint(
        "uq_generated_files_task",
        "generated_files",
        ["task_id"],
        schema="agent",
    )
