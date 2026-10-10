"""Lifecycle confirmations and bound resource snapshots."""

from alembic import op

revision = "0006_lifecycle"
down_revision = "0005_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE agent.pending_actions (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    origin VARCHAR(20) NOT NULL,
    operation_name VARCHAR(200) NOT NULL,
    agent_run_id BIGINT,
    tool_call_id BIGINT,
    actor_user_id BIGINT NOT NULL,
    idempotency_key VARCHAR(200) NOT NULL,
    action_type VARCHAR(30) NOT NULL,
    prompt_text TEXT NOT NULL,
    bound_arguments JSONB NOT NULL,
    binding_hash CHAR(64) NOT NULL,
    status VARCHAR(20) DEFAULT 'pending' NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    response JSONB,
    resolved_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_pending_actor_key UNIQUE (actor_user_id, idempotency_key),
    CONSTRAINT ck_pending_type CHECK (action_type IN ('confirmation','disambiguation')),
    CONSTRAINT ck_pending_status CHECK (status IN
        ('pending','accepted','rejected','expired','cancelled')),
    CONSTRAINT ck_pending_origin CHECK ((origin = 'api' AND agent_run_id IS NULL AND
        tool_call_id IS NULL AND action_type = 'confirmation') OR (origin = 'agent' AND
        agent_run_id IS NOT NULL AND (action_type = 'disambiguation' OR tool_call_id IS NOT
        NULL))),
    CONSTRAINT ck_pending_times CHECK (expires_at > created_at AND (status = 'pending' OR
        resolved_at IS NOT NULL)),
    FOREIGN KEY(agent_run_id) REFERENCES agent.agent_runs (id) ON DELETE CASCADE,
    FOREIGN KEY(tool_call_id) REFERENCES agent.tool_calls (id) ON DELETE CASCADE,
    FOREIGN KEY(actor_user_id) REFERENCES iam.users (id) ON DELETE RESTRICT
);
CREATE INDEX ix_pending_expiry ON agent.pending_actions (status, expires_at);
CREATE UNIQUE INDEX uq_pending_run ON agent.pending_actions (agent_run_id) WHERE status =
        'pending' AND agent_run_id IS NOT NULL;
CREATE TABLE agent.pending_action_targets (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    pending_action_id BIGINT NOT NULL,
    ordinal INTEGER NOT NULL,
    resource_type VARCHAR(30) NOT NULL,
    resource_id BIGINT NOT NULL,
    display_name VARCHAR(500) NOT NULL,
    expected_row_version BIGINT,
    target_snapshot JSONB NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_pending_target_ordinal UNIQUE (pending_action_id, ordinal),
    CONSTRAINT ck_pending_target_values CHECK (ordinal >= 0 AND (expected_row_version IS NULL
        OR expected_row_version > 0)),
    FOREIGN KEY(pending_action_id) REFERENCES agent.pending_actions (id) ON DELETE CASCADE
);
    """)


def downgrade() -> None:
    raise RuntimeError("Rollback requires an explicit data recovery plan.")
