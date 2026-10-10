"""Add fenced execution leases to generation tasks.

Each worker attempt owns a short-lived lease. A restarted worker may reclaim a
task only after its previous lease expires; the owner token fences late work
from publishing after a newer attempt has claimed the task. Stop every 0014
worker before applying this migration, then start only 0015-aware workers:
unfenced 0014 workers cannot safely share running tasks with the new protocol.
Legacy running rows with a null lease are reclaimable only after those workers
have stopped.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_generation_execution_lease"
down_revision = "0014_phase10_chart_bundle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_tasks",
        sa.Column("execution_owner", sa.String(length=64), nullable=True),
        schema="agent",
    )
    op.add_column(
        "generation_tasks",
        sa.Column("execution_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        schema="agent",
    )
    op.add_column(
        "generation_tasks",
        sa.Column(
            "execution_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        schema="agent",
    )
    op.create_check_constraint(
        "ck_generation_tasks_execution_attempt_count",
        "generation_tasks",
        "execution_attempt_count >= 0",
        schema="agent",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_generation_tasks_execution_attempt_count",
        "generation_tasks",
        schema="agent",
        type_="check",
    )
    op.drop_column("generation_tasks", "execution_attempt_count", schema="agent")
    op.drop_column("generation_tasks", "execution_lease_expires_at", schema="agent")
    op.drop_column("generation_tasks", "execution_owner", schema="agent")
