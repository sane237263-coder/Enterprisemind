"""Allow CSV for ordinary read-only database exports."""

from alembic import op

revision = "0016_database_csv_export"
down_revision = "0015_generation_execution_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_database_export_plans_format",
        "database_export_plans",
        schema="agent",
        type_="check",
    )
    op.create_check_constraint(
        "ck_database_export_plans_format",
        "database_export_plans",
        "format IN ('xlsx','csv')",
        schema="agent",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_database_export_plans_format",
        "database_export_plans",
        schema="agent",
        type_="check",
    )
    op.create_check_constraint(
        "ck_database_export_plans_format",
        "database_export_plans",
        "format = 'xlsx'",
        schema="agent",
    )
