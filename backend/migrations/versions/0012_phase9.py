"""Stage 9: personal memory, frozen drafts and private generation records."""

from alembic import op

revision = "0012_phase9"
down_revision = "0011_database_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE agent.user_memory_states (
    user_id BIGINT NOT NULL,
    memory_epoch BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id),
    CONSTRAINT ck_user_memory_states_epoch CHECK (memory_epoch >= 0),
    CONSTRAINT fk_user_memory_states_user FOREIGN KEY (user_id)
        REFERENCES iam.users(id) ON DELETE CASCADE
);

CREATE TABLE agent.personal_memories (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    user_id BIGINT NOT NULL,
    category VARCHAR(30) NOT NULL,
    subject_key VARCHAR(300),
    content TEXT NOT NULL,
    content_hash CHAR(64),
    origin VARCHAR(20) NOT NULL,
    source_message_id BIGINT,
    revision BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT ck_personal_memories_category CHECK
        (category IN ('preference','work_context','ongoing_context','other')),
    CONSTRAINT ck_personal_memories_origin CHECK
        (origin IN ('explicit','manual','automatic')),
    CONSTRAINT ck_personal_memories_content CHECK
        (length(trim(content)) > 0 AND char_length(content) <= 1000),
    CONSTRAINT ck_personal_memories_revision CHECK (revision >= 1),
    CONSTRAINT fk_personal_memories_user FOREIGN KEY (user_id)
        REFERENCES iam.users(id) ON DELETE CASCADE,
    CONSTRAINT fk_personal_memories_message FOREIGN KEY (source_message_id)
        REFERENCES agent.messages(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX uq_personal_memories_subject
    ON agent.personal_memories(user_id, subject_key)
    WHERE subject_key IS NOT NULL;
CREATE INDEX ix_personal_memories_user_updated
    ON agent.personal_memories(user_id, updated_at DESC, id DESC);
CREATE INDEX ix_personal_memories_user_category
    ON agent.personal_memories(user_id, category, updated_at DESC, id DESC);

CREATE TABLE agent.content_drafts (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    owner_user_id BIGINT NOT NULL,
    conversation_id BIGINT,
    source_message_id BIGINT,
    title VARCHAR(300) NOT NULL,
    body_markdown TEXT NOT NULL,
    tables JSONB,
    source_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    revision BIGINT NOT NULL DEFAULT 1,
    content_hash CHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'ready',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT ck_content_drafts_revision CHECK (revision >= 1),
    CONSTRAINT ck_content_drafts_status CHECK (status IN ('draft','ready')),
    CONSTRAINT ck_content_drafts_body CHECK (length(body_markdown) > 0),
    CONSTRAINT fk_content_drafts_user FOREIGN KEY (owner_user_id)
        REFERENCES iam.users(id) ON DELETE RESTRICT,
    CONSTRAINT fk_content_drafts_conversation FOREIGN KEY (conversation_id)
        REFERENCES agent.conversations(id) ON DELETE SET NULL,
    CONSTRAINT fk_content_drafts_message FOREIGN KEY (source_message_id)
        REFERENCES agent.messages(id) ON DELETE SET NULL
);
CREATE INDEX ix_content_drafts_owner_updated
    ON agent.content_drafts(owner_user_id, updated_at DESC, id DESC);

CREATE TABLE agent.database_export_plans (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    owner_user_id BIGINT NOT NULL,
    conversation_id BIGINT NOT NULL,
    query_id VARCHAR(64),
    result_id VARCHAR(64),
    runtime_handle VARCHAR(64) NOT NULL,
    scope VARCHAR(20) NOT NULL,
    format VARCHAR(20) NOT NULL,
    database_source_id BIGINT NOT NULL,
    schema_snapshot_id BIGINT NOT NULL,
    source_version BIGINT NOT NULL,
    permission_fingerprint VARCHAR(128) NOT NULL,
    catalog JSONB NOT NULL,
    referenced_relations JSONB NOT NULL,
    conditions_summary TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT ck_database_export_plans_scope CHECK
        (scope IN ('current_page','all_matching','first_n')),
    CONSTRAINT ck_database_export_plans_format CHECK (format = 'xlsx'),
    CONSTRAINT ck_database_export_plans_version CHECK (source_version >= 1),
    CONSTRAINT fk_database_export_plans_user FOREIGN KEY (owner_user_id)
        REFERENCES iam.users(id) ON DELETE RESTRICT,
    CONSTRAINT fk_database_export_plans_conversation FOREIGN KEY (conversation_id)
        REFERENCES agent.conversations(id) ON DELETE CASCADE
);
CREATE INDEX ix_database_export_plans_expiry
    ON agent.database_export_plans(owner_user_id, expires_at);

CREATE TABLE agent.generation_tasks (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    owner_user_id BIGINT NOT NULL,
    conversation_id BIGINT,
    kind VARCHAR(30) NOT NULL,
    format VARCHAR(20) NOT NULL,
    scope VARCHAR(20),
    draft_id BIGINT,
    source_message_id BIGINT,
    export_plan_id BIGINT,
    idempotency_key VARCHAR(200) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    input_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    phase VARCHAR(40),
    processed_rows BIGINT NOT NULL DEFAULT 0,
    total_rows BIGINT,
    exported_rows BIGINT,
    file_id BIGINT,
    error_code VARCHAR(100),
    error_message TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT uq_generation_task_idempotency UNIQUE (owner_user_id, idempotency_key),
    CONSTRAINT ck_generation_tasks_kind CHECK (kind IN ('content_render','database_export')),
    CONSTRAINT ck_generation_tasks_format CHECK (format IN ('docx','pdf','md','xlsx')),
    CONSTRAINT ck_generation_tasks_scope CHECK
        (scope IS NULL OR scope IN ('current_page','all_matching','first_n')),
    CONSTRAINT ck_generation_tasks_status CHECK
        (status IN ('queued','running','succeeded','failed','cancelled')),
    CONSTRAINT ck_generation_tasks_rows CHECK
        (processed_rows >= 0 AND (total_rows IS NULL OR total_rows >= 0)
         AND (exported_rows IS NULL OR exported_rows >= 0)),
    CONSTRAINT fk_generation_tasks_user FOREIGN KEY (owner_user_id)
        REFERENCES iam.users(id) ON DELETE RESTRICT,
    CONSTRAINT fk_generation_tasks_conversation FOREIGN KEY (conversation_id)
        REFERENCES agent.conversations(id) ON DELETE SET NULL,
    CONSTRAINT fk_generation_tasks_draft FOREIGN KEY (draft_id)
        REFERENCES agent.content_drafts(id) ON DELETE SET NULL,
    CONSTRAINT fk_generation_tasks_message FOREIGN KEY (source_message_id)
        REFERENCES agent.messages(id) ON DELETE SET NULL,
    CONSTRAINT fk_generation_tasks_plan FOREIGN KEY (export_plan_id)
        REFERENCES agent.database_export_plans(id) ON DELETE SET NULL,
    CONSTRAINT fk_generation_tasks_file FOREIGN KEY (file_id)
        REFERENCES infra.storage_objects(id) ON DELETE SET NULL
);
CREATE INDEX ix_generation_tasks_owner_created
    ON agent.generation_tasks(owner_user_id, created_at DESC, id DESC);
CREATE INDEX ix_generation_tasks_recovery
    ON agent.generation_tasks(status, updated_at, id);

CREATE TABLE agent.generated_files (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    task_id BIGINT NOT NULL,
    owner_user_id BIGINT NOT NULL,
    conversation_id BIGINT,
    storage_object_id BIGINT NOT NULL,
    file_name VARCHAR(500) NOT NULL,
    file_format VARCHAR(20) NOT NULL,
    generation_kind VARCHAR(30) NOT NULL,
    draft_id BIGINT,
    source_message_id BIGINT,
    source_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    result_scope VARCHAR(20),
    exported_rows BIGINT,
    content_sha256 CHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT uq_generated_files_task UNIQUE (task_id),
    CONSTRAINT uq_generated_files_object UNIQUE (storage_object_id),
    CONSTRAINT ck_generated_files_format CHECK (file_format IN ('docx','pdf','md','xlsx')),
    CONSTRAINT ck_generated_files_kind CHECK
        (generation_kind IN ('content_render','database_export')),
    CONSTRAINT ck_generated_files_size CHECK (size_bytes >= 0),
    CONSTRAINT fk_generated_files_task FOREIGN KEY (task_id)
        REFERENCES agent.generation_tasks(id) ON DELETE CASCADE,
    CONSTRAINT fk_generated_files_user FOREIGN KEY (owner_user_id)
        REFERENCES iam.users(id) ON DELETE RESTRICT,
    CONSTRAINT fk_generated_files_conversation FOREIGN KEY (conversation_id)
        REFERENCES agent.conversations(id) ON DELETE SET NULL,
    CONSTRAINT fk_generated_files_object FOREIGN KEY (storage_object_id)
        REFERENCES infra.storage_objects(id) ON DELETE RESTRICT,
    CONSTRAINT fk_generated_files_draft FOREIGN KEY (draft_id)
        REFERENCES agent.content_drafts(id) ON DELETE SET NULL,
    CONSTRAINT fk_generated_files_message FOREIGN KEY (source_message_id)
        REFERENCES agent.messages(id) ON DELETE SET NULL
);
CREATE INDEX ix_generated_files_owner_created
    ON agent.generated_files(owner_user_id, generated_at DESC, id DESC);
"""
    )


def downgrade() -> None:
    raise RuntimeError("Rollback requires an explicit data recovery plan.")
