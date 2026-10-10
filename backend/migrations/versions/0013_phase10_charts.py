"""Allow the bounded Phase 10 chart generation task.

The Phase 9 tables remain the source of truth.  This migration only widens
their checks; SQL, parameters, rows and chart snapshots remain process local.
"""

from alembic import op

revision = "0013_phase10_charts"
down_revision = "0012_phase9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_generation_tasks_kind", "generation_tasks", schema="agent")
    op.create_check_constraint(
        "ck_generation_tasks_kind",
        "generation_tasks",
        "kind IN ('content_render','database_export','chart_render')",
        schema="agent",
    )
    op.drop_constraint("ck_generation_tasks_format", "generation_tasks", schema="agent")
    op.create_check_constraint(
        "ck_generation_tasks_format",
        "generation_tasks",
        "format IN ('docx','pdf','md','xlsx','png','csv')",
        schema="agent",
    )
    op.drop_constraint("ck_generation_tasks_scope", "generation_tasks", schema="agent")
    op.create_check_constraint(
        "ck_generation_tasks_scope",
        "generation_tasks",
        "scope IS NULL OR scope IN ('current_page','all_matching','first_n','top_n')",
        schema="agent",
    )
    op.drop_constraint("ck_generated_files_format", "generated_files", schema="agent")
    op.create_check_constraint(
        "ck_generated_files_format",
        "generated_files",
        "file_format IN ('docx','pdf','md','xlsx','png','csv')",
        schema="agent",
    )
    op.drop_constraint("ck_generated_files_kind", "generated_files", schema="agent")
    op.create_check_constraint(
        "ck_generated_files_kind",
        "generated_files",
        "generation_kind IN ('content_render','database_export','chart_render')",
        schema="agent",
    )


def downgrade() -> None:
    op.drop_constraint("ck_generated_files_kind", "generated_files", schema="agent")
    op.create_check_constraint(
        "ck_generated_files_kind",
        "generated_files",
        "generation_kind IN ('content_render','database_export')",
        schema="agent",
    )
    op.drop_constraint("ck_generated_files_format", "generated_files", schema="agent")
    op.create_check_constraint(
        "ck_generated_files_format",
        "generated_files",
        "file_format IN ('docx','pdf','md','xlsx')",
        schema="agent",
    )
    op.drop_constraint("ck_generation_tasks_scope", "generation_tasks", schema="agent")
    op.create_check_constraint(
        "ck_generation_tasks_scope",
        "generation_tasks",
        "scope IS NULL OR scope IN ('current_page','all_matching','first_n')",
        schema="agent",
    )
    op.drop_constraint("ck_generation_tasks_format", "generation_tasks", schema="agent")
    op.create_check_constraint(
        "ck_generation_tasks_format",
        "generation_tasks",
        "format IN ('docx','pdf','md','xlsx')",
        schema="agent",
    )
    op.drop_constraint("ck_generation_tasks_kind", "generation_tasks", schema="agent")
    op.create_check_constraint(
        "ck_generation_tasks_kind",
        "generation_tasks",
        "kind IN ('content_render','database_export')",
        schema="agent",
    )
