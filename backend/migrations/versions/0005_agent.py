"""Conversation and agent business state, separate from framework checkpoints."""

from alembic import op

revision = "0005_agent"
down_revision = "0004_retrieval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE agent.conversations (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    owner_user_id BIGINT NOT NULL,
    title VARCHAR(300) NOT NULL,
    next_message_sequence BIGINT DEFAULT '1' NOT NULL,
    last_message_at TIMESTAMP WITH TIME ZONE,
    row_version BIGINT DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_conversation_owner UNIQUE (id, owner_user_id),
    CONSTRAINT ck_conversation_counts CHECK (next_message_sequence > 0 AND row_version > 0),
    FOREIGN KEY(owner_user_id) REFERENCES iam.users (id) ON DELETE RESTRICT
);

    CREATE INDEX ix_conversations_owner_recent ON agent.conversations (owner_user_id,
        last_message_at, id);

CREATE TABLE agent.messages (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    conversation_id BIGINT NOT NULL,
    sequence BIGINT NOT NULL,
    role VARCHAR(20) NOT NULL,
    message_type VARCHAR(30) DEFAULT 'text' NOT NULL,
    content_text TEXT,
    content_parts JSONB,
    status VARCHAR(20) NOT NULL,
    token_count INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT uq_message_sequence UNIQUE (conversation_id, sequence),
    CONSTRAINT uq_message_conversation UNIQUE (id, conversation_id),
    CONSTRAINT ck_message_role CHECK (role IN ('user','assistant')),
    CONSTRAINT ck_message_status CHECK (status IN ('streaming','completed','failed','stopped')),
    CONSTRAINT ck_message_type CHECK (message_type IN ('text','mixed','status','error')),
    CONSTRAINT ck_message_counts CHECK (sequence > 0 AND (token_count IS NULL OR token_count >= 0)),
    FOREIGN KEY(conversation_id) REFERENCES agent.conversations (id) ON DELETE CASCADE
);

CREATE TABLE agent.conversation_summaries (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    conversation_id BIGINT NOT NULL,
    through_sequence BIGINT NOT NULL,
    summary_text TEXT NOT NULL,
    source_token_count INTEGER,
    summary_token_count INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_summary_sequence UNIQUE (conversation_id, through_sequence),
    FOREIGN KEY(conversation_id) REFERENCES agent.conversations (id) ON DELETE CASCADE
);

CREATE TABLE agent.agent_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    conversation_id BIGINT NOT NULL,
    user_message_id BIGINT NOT NULL,
    assistant_message_id BIGINT,
    actor_user_id BIGINT NOT NULL,
    status VARCHAR(30) DEFAULT 'pending' NOT NULL,
    current_step VARCHAR(30),
    step_message VARCHAR(500),
    permission_scope_version VARCHAR(100) NOT NULL,
    tool_call_count INTEGER DEFAULT '0' NOT NULL,
    replan_count INTEGER DEFAULT '0' NOT NULL,
    retry_count INTEGER DEFAULT '0' NOT NULL,
    cancel_requested_at TIMESTAMP WITH TIME ZONE,
    failure_code VARCHAR(100),
    failure_message TEXT,
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_run_conversation_owner FOREIGN KEY(conversation_id, actor_user_id) REFERENCES
        agent.conversations (id, owner_user_id) ON DELETE CASCADE,
    CONSTRAINT fk_run_user_message FOREIGN KEY(user_message_id, conversation_id) REFERENCES
        agent.messages (id, conversation_id) ON DELETE CASCADE,
    CONSTRAINT fk_run_assistant_message FOREIGN KEY(assistant_message_id, conversation_id)
        REFERENCES agent.messages (id, conversation_id) ON DELETE SET NULL
        (assistant_message_id),
    CONSTRAINT uq_run_user_message UNIQUE (user_message_id),
    CONSTRAINT uq_run_assistant_message UNIQUE (assistant_message_id),
    CONSTRAINT ck_run_status CHECK (status IN
        ('pending','running','waiting_confirmation','cancelled','completed','failed')),
    CONSTRAINT ck_run_counts CHECK (tool_call_count >= 0 AND replan_count >= 0 AND retry_count
        >= 0),
    CONSTRAINT ck_run_terminal_time CHECK (status NOT IN ('completed','failed','cancelled') OR
        finished_at IS NOT NULL),
    FOREIGN KEY(actor_user_id) REFERENCES iam.users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_runs_recovery ON agent.agent_runs (status, updated_at, id);

    CREATE UNIQUE INDEX uq_conversation_active_run ON agent.agent_runs (conversation_id) WHERE
        status IN ('pending','running','waiting_confirmation');

CREATE TABLE agent.tool_calls (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    agent_run_id BIGINT NOT NULL,
    actor_user_id BIGINT NOT NULL,
    tool_name VARCHAR(200) NOT NULL,
    arguments JSONB NOT NULL,
    arguments_hash VARCHAR(64) NOT NULL,
    permission_scope_version VARCHAR(100) NOT NULL,
    idempotency_key VARCHAR(200) NOT NULL,
    risk_level VARCHAR(20) DEFAULT 'low' NOT NULL,
    status VARCHAR(30) DEFAULT 'pending' NOT NULL,
    retry_count INTEGER DEFAULT '0' NOT NULL,
    result_summary JSONB,
    error_code VARCHAR(100),
    error_message TEXT,
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_tool_idempotency UNIQUE (actor_user_id, idempotency_key),
    CONSTRAINT ck_tool_status CHECK (status IN
        ('pending','waiting_confirmation','running','success','failed','cancelled')),
    CONSTRAINT ck_tool_limits CHECK (risk_level IN ('low','high') AND retry_count >= 0),
    FOREIGN KEY(agent_run_id) REFERENCES agent.agent_runs (id) ON DELETE CASCADE,
    FOREIGN KEY(actor_user_id) REFERENCES iam.users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_tool_run ON agent.tool_calls (agent_run_id, id);

CREATE TABLE agent.answer_sources (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    message_id BIGINT NOT NULL,
    ordinal INTEGER NOT NULL,
    source_type VARCHAR(20) DEFAULT 'knowledge' NOT NULL,
    chunk_id BIGINT,
    anchor_id BIGINT,
    source_title VARCHAR(500) NOT NULL,
    quote_text TEXT,
    locator_snapshot JSONB,
    relevance JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_answer_source_ordinal UNIQUE (message_id, ordinal),
    CONSTRAINT ck_answer_source_type CHECK (source_type = 'knowledge' AND ordinal > 0),
    FOREIGN KEY(message_id) REFERENCES agent.messages (id) ON DELETE CASCADE,
    FOREIGN KEY(chunk_id) REFERENCES knowledge.chunks (id) ON DELETE SET NULL,
    FOREIGN KEY(anchor_id) REFERENCES knowledge.source_anchors (id) ON DELETE SET NULL
);
""")
    op.execute("""
CREATE FUNCTION agent.validate_run_messages() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, agent AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM agent.messages WHERE id=NEW.user_message_id AND role='user')
       OR (NEW.assistant_message_id IS NOT NULL AND NOT EXISTS
           (SELECT 1 FROM agent.messages WHERE id=NEW.assistant_message_id AND role='assistant'))
    THEN RAISE EXCEPTION 'invalid run message role' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION agent.validate_run_messages() FROM PUBLIC;
CREATE TRIGGER validate_run_messages BEFORE INSERT OR UPDATE ON agent.agent_runs
FOR EACH ROW EXECUTE FUNCTION agent.validate_run_messages();
CREATE FUNCTION agent.validate_answer_source() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, agent, knowledge AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM agent.messages WHERE id=NEW.message_id AND role='assistant')
       OR NEW.chunk_id IS NULL OR NEW.anchor_id IS NULL
       OR NOT EXISTS (
           SELECT 1 FROM knowledge.chunk_anchors ca WHERE ca.chunk_id=NEW.chunk_id
           AND ca.anchor_id=NEW.anchor_id AND NEW.quote_text <> ''
           AND position(NEW.quote_text IN ca.quote_text)>0
       )
    THEN RAISE EXCEPTION 'invalid answer source' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION agent.validate_answer_source() FROM PUBLIC;
CREATE TRIGGER validate_answer_source BEFORE INSERT ON agent.answer_sources
FOR EACH ROW EXECUTE FUNCTION agent.validate_answer_source();
""")


def downgrade() -> None:
    raise RuntimeError("Rollback requires an explicit data recovery plan.")
