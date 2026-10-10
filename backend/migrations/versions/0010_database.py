"""Stage 8: the single Database MCP source, schema snapshots and grants."""

from alembic import op

revision = "0010_database"
down_revision = "0009_preview_rebuild"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Database answer sources are deliberately added in the same migration as
    # the integration schema so both sides of the optional foreign keys exist.
    op.execute(
        """
ALTER TABLE agent.answer_sources
    ADD COLUMN database_tool_call_id BIGINT,
    ADD COLUMN database_source_id BIGINT,
    ADD COLUMN schema_snapshot_id BIGINT,
    ADD COLUMN database_query_id VARCHAR(64),
    ADD COLUMN database_result_id VARCHAR(64),
    ADD COLUMN database_query_kind VARCHAR(20),
    ADD COLUMN database_row_count INTEGER,
    ADD COLUMN referenced_relations JSONB,
    ADD COLUMN queried_at TIMESTAMPTZ,
    ADD COLUMN conditions_summary TEXT,
    ADD COLUMN has_more BOOLEAN;
ALTER TABLE agent.answer_sources
    DROP CONSTRAINT IF EXISTS ck_answer_source_type;
ALTER TABLE agent.answer_sources
    ADD CONSTRAINT ck_answer_source_type CHECK (
        (source_type = 'knowledge' AND ordinal > 0
            AND database_tool_call_id IS NULL AND database_source_id IS NULL
            AND schema_snapshot_id IS NULL AND referenced_relations IS NULL
            AND database_query_id IS NULL AND database_result_id IS NULL
            AND database_query_kind IS NULL AND database_row_count IS NULL
            AND queried_at IS NULL AND conditions_summary IS NULL AND has_more IS NULL
            AND source_title <> ''
            AND locator_snapshot IS NOT NULL
            AND jsonb_typeof(locator_snapshot) = 'object')
        OR
        (source_type = 'database' AND ordinal > 0
            AND chunk_id IS NULL AND anchor_id IS NULL
            AND jsonb_typeof(referenced_relations) = 'array'
            AND jsonb_array_length(
                CASE WHEN jsonb_typeof(referenced_relations) = 'array'
                     THEN referenced_relations ELSE '[]'::jsonb END
            ) > 0
            AND database_query_id IS NOT NULL AND length(database_query_id) > 0
            AND database_result_id IS NOT NULL AND length(database_result_id) > 0
            AND database_query_kind IN ('detail', 'aggregate')
            AND database_row_count IS NOT NULL AND database_row_count >= 0
            AND queried_at IS NOT NULL AND conditions_summary IS NOT NULL
            AND length(conditions_summary) > 0 AND has_more IS NOT NULL
            AND source_title <> ''
            AND quote_text IS NULL AND relevance IS NULL
            AND locator_snapshot IS NOT NULL
            AND jsonb_typeof(locator_snapshot) = 'object'
            AND (locator_snapshot ? 'data_source_name'
                OR locator_snapshot ? 'database_source_name')
            AND locator_snapshot ? 'database_name')
    );
"""
    )

    op.execute(
        """
CREATE TABLE integration.database_sources (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    name VARCHAR(200) NOT NULL,
    host VARCHAR(255) NOT NULL,
    port INTEGER NOT NULL,
    database_name VARCHAR(200) NOT NULL,
    ssl_mode VARCHAR(30) NOT NULL,
    secret_ref VARCHAR(500) NOT NULL,
    connection_options JSONB,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    active_snapshot_id BIGINT,
    row_version BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT uq_database_sources_name UNIQUE (name),
    CONSTRAINT ck_database_sources_status CHECK (status IN ('active','disabled','error')),
    CONSTRAINT ck_database_sources_port CHECK (port BETWEEN 1 AND 65535),
    CONSTRAINT ck_database_sources_row_version CHECK (row_version >= 1),
    CONSTRAINT ck_database_sources_names CHECK (length(name) > 0 AND length(host) > 0),
    CONSTRAINT ck_database_sources_database_name CHECK (length(database_name) > 0),
    CONSTRAINT ck_database_sources_secret_ref CHECK (length(secret_ref) > 0)
);
CREATE UNIQUE INDEX uq_database_sources_singleton ON integration.database_sources ((true));
CREATE INDEX ix_database_sources_active_snapshot
    ON integration.database_sources (active_snapshot_id);

CREATE TABLE integration.schema_snapshots (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    database_source_id BIGINT NOT NULL,
    snapshot_no BIGINT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'building',
    captured_at TIMESTAMPTZ,
    error_code VARCHAR(100),
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT uq_schema_snapshots_source_no UNIQUE (database_source_id, snapshot_no),
    CONSTRAINT uq_schema_snapshots_id_source UNIQUE (id, database_source_id),
    CONSTRAINT ck_schema_snapshots_no CHECK (snapshot_no >= 1),
    CONSTRAINT ck_schema_snapshots_status CHECK
        (status IN ('building','ready','active','failed','retired')),
    CONSTRAINT ck_schema_snapshots_captured CHECK
        (status NOT IN ('ready','active') OR captured_at IS NOT NULL),
    CONSTRAINT fk_schema_snapshots_source FOREIGN KEY (database_source_id)
        REFERENCES integration.database_sources(id) ON DELETE RESTRICT
);
CREATE INDEX ix_schema_snapshots_source_status
    ON integration.schema_snapshots (database_source_id, status);
CREATE UNIQUE INDEX uq_schema_snapshots_active
    ON integration.schema_snapshots (database_source_id) WHERE status = 'active';

CREATE TABLE integration.schema_relations (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    snapshot_id BIGINT NOT NULL,
    schema_name VARCHAR(200) NOT NULL,
    relation_name VARCHAR(200) NOT NULL,
    relation_type VARCHAR(20) NOT NULL,
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT uq_schema_relations_name UNIQUE (snapshot_id, schema_name, relation_name),
    CONSTRAINT ck_schema_relations_type CHECK (relation_type IN ('table','view')),
    CONSTRAINT fk_schema_relations_snapshot FOREIGN KEY (snapshot_id)
        REFERENCES integration.schema_snapshots(id) ON DELETE CASCADE
);
CREATE INDEX ix_schema_relations_snapshot ON integration.schema_relations (snapshot_id, id);

CREATE TABLE integration.schema_columns (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    relation_id BIGINT NOT NULL,
    ordinal INTEGER NOT NULL,
    column_name VARCHAR(200) NOT NULL,
    data_type VARCHAR(300) NOT NULL,
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT uq_schema_columns_name UNIQUE (relation_id, column_name),
    CONSTRAINT uq_schema_columns_ordinal UNIQUE (relation_id, ordinal),
    CONSTRAINT ck_schema_columns_ordinal CHECK (ordinal >= 1),
    CONSTRAINT fk_schema_columns_relation FOREIGN KEY (relation_id)
        REFERENCES integration.schema_relations(id) ON DELETE CASCADE
);
CREATE INDEX ix_schema_columns_relation ON integration.schema_columns (relation_id, ordinal);

CREATE TABLE integration.schema_constraints (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    relation_id BIGINT NOT NULL,
    constraint_name VARCHAR(300) NOT NULL,
    constraint_type VARCHAR(20) NOT NULL,
    column_names TEXT[] NOT NULL,
    referenced_schema_name VARCHAR(200),
    referenced_relation_name VARCHAR(200),
    referenced_column_names TEXT[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT uq_schema_constraints_name UNIQUE (relation_id, constraint_name),
    CONSTRAINT ck_schema_constraints_type CHECK
        (constraint_type IN ('primary_key','foreign_key')),
    CONSTRAINT ck_schema_constraints_columns CHECK (cardinality(column_names) > 0),
    CONSTRAINT ck_schema_constraints_reference CHECK (
        (constraint_type = 'primary_key' AND referenced_schema_name IS NULL
            AND referenced_relation_name IS NULL AND referenced_column_names IS NULL)
        OR
        (constraint_type = 'foreign_key' AND referenced_schema_name IS NOT NULL
            AND referenced_relation_name IS NOT NULL AND referenced_column_names IS NOT NULL
            AND cardinality(referenced_column_names) = cardinality(column_names))
    ),
    CONSTRAINT fk_schema_constraints_relation FOREIGN KEY (relation_id)
        REFERENCES integration.schema_relations(id) ON DELETE CASCADE
);
CREATE INDEX ix_schema_constraints_relation ON integration.schema_constraints (relation_id);

CREATE TABLE integration.relation_policies (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    database_source_id BIGINT NOT NULL,
    schema_name VARCHAR(200) NOT NULL,
    relation_name VARCHAR(200) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT false,
    row_version BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT uq_relation_policies_name UNIQUE
        (database_source_id, schema_name, relation_name),
    CONSTRAINT ck_relation_policies_row_version CHECK (row_version >= 1),
    CONSTRAINT fk_relation_policies_source FOREIGN KEY (database_source_id)
        REFERENCES integration.database_sources(id) ON DELETE RESTRICT
);
CREATE INDEX ix_relation_policies_source_enabled
    ON integration.relation_policies (database_source_id, enabled);

CREATE TABLE integration.relation_department_grants (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    relation_policy_id BIGINT NOT NULL,
    department_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT uq_relation_department_grants UNIQUE (relation_policy_id, department_id),
    CONSTRAINT fk_relation_department_grants_policy FOREIGN KEY (relation_policy_id)
        REFERENCES integration.relation_policies(id) ON DELETE CASCADE,
    CONSTRAINT fk_relation_department_grants_department FOREIGN KEY (department_id)
        REFERENCES iam.departments(id) ON DELETE CASCADE
);
CREATE INDEX ix_relation_department_grants_department
    ON integration.relation_department_grants (department_id, relation_policy_id);

ALTER TABLE integration.database_sources
    ADD CONSTRAINT fk_database_sources_active_snapshot
    FOREIGN KEY (active_snapshot_id, id)
    REFERENCES integration.schema_snapshots (id, database_source_id)
    ON DELETE SET NULL (active_snapshot_id);

ALTER TABLE agent.answer_sources
    ADD CONSTRAINT fk_answer_sources_database_tool_call
        FOREIGN KEY (database_tool_call_id)
        REFERENCES agent.tool_calls(id) ON DELETE SET NULL,
    ADD CONSTRAINT fk_answer_sources_database_source
        FOREIGN KEY (database_source_id)
        REFERENCES integration.database_sources(id) ON DELETE SET NULL,
    ADD CONSTRAINT fk_answer_sources_schema_snapshot
        FOREIGN KEY (schema_snapshot_id)
        REFERENCES integration.schema_snapshots(id) ON DELETE SET NULL;
CREATE INDEX ix_answer_sources_database_source
    ON agent.answer_sources (database_source_id, schema_snapshot_id);
"""
    )

    # The stage-5 trigger only knew about knowledge chunks.  Keep its strong
    # chunk/anchor check while allowing a database source to carry the new
    # source-specific foreign keys and locator snapshot.
    op.execute(
        """
CREATE OR REPLACE FUNCTION agent.validate_answer_source() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, agent, knowledge AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM agent.messages WHERE id = NEW.message_id AND role = 'assistant'
    ) THEN
        RAISE EXCEPTION 'invalid answer source' USING ERRCODE = '23514';
    END IF;
    IF NEW.source_type = 'knowledge' THEN
        IF NEW.chunk_id IS NULL OR NEW.anchor_id IS NULL OR NEW.quote_text IS NULL
           OR NEW.quote_text = '' OR NEW.source_title = ''
           OR NEW.locator_snapshot IS NULL
           OR jsonb_typeof(NEW.locator_snapshot) <> 'object'
           OR NOT EXISTS (
               SELECT 1 FROM knowledge.chunk_anchors ca
               WHERE ca.chunk_id = NEW.chunk_id AND ca.anchor_id = NEW.anchor_id
                 AND position(NEW.quote_text IN ca.quote_text) > 0
           ) THEN
            RAISE EXCEPTION 'invalid answer source' USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.source_type = 'database' THEN
        IF NEW.database_tool_call_id IS NULL OR NEW.database_source_id IS NULL
           OR NEW.schema_snapshot_id IS NULL OR NEW.referenced_relations IS NULL
           OR NEW.database_query_id IS NULL OR NEW.database_result_id IS NULL
           OR NEW.database_query_kind NOT IN ('detail', 'aggregate')
           OR NEW.database_row_count IS NULL OR NEW.database_row_count < 0
           OR jsonb_typeof(NEW.referenced_relations) <> 'array'
           OR jsonb_array_length(
                CASE WHEN jsonb_typeof(NEW.referenced_relations) = 'array'
                     THEN NEW.referenced_relations ELSE '[]'::jsonb END
              ) = 0
           OR NEW.queried_at IS NULL OR NEW.conditions_summary IS NULL
           OR NEW.conditions_summary = '' OR NEW.has_more IS NULL
           OR NEW.source_title = ''
           OR NEW.quote_text IS NOT NULL OR NEW.relevance IS NOT NULL
           OR NEW.locator_snapshot IS NULL
           OR NOT (NEW.locator_snapshot ? 'data_source_name')
              AND NOT (NEW.locator_snapshot ? 'database_source_name')
           OR NOT (NEW.locator_snapshot ? 'database_name') THEN
            RAISE EXCEPTION 'invalid answer source' USING ERRCODE = '23514';
        END IF;
        IF NOT EXISTS (
            SELECT 1
            FROM agent.tool_calls tc
            JOIN agent.agent_runs ar ON ar.id = tc.agent_run_id
            WHERE tc.id = NEW.database_tool_call_id
              AND tc.tool_name = 'database_query'
              AND ar.assistant_message_id = NEW.message_id
        ) THEN
            RAISE EXCEPTION 'invalid database answer source tool' USING ERRCODE = '23514';
        END IF;
        IF NOT EXISTS (
            SELECT 1
            FROM integration.schema_snapshots ss
            WHERE ss.id = NEW.schema_snapshot_id
              AND ss.database_source_id = NEW.database_source_id
        ) THEN
            RAISE EXCEPTION 'invalid database answer source snapshot' USING ERRCODE = '23514';
        END IF;
    ELSE
        RAISE EXCEPTION 'invalid answer source' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION agent.validate_answer_source() FROM PUBLIC;
"""
    )

    op.execute(
        """
CREATE OR REPLACE FUNCTION integration.validate_database_source_snapshot() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, integration AS $$
BEGIN
    IF TG_TABLE_NAME = 'database_sources' THEN
        -- Deferred events retain each intermediate NEW row. Check the final
        -- row at commit, so building -> ready -> active in one transaction is valid.
        SELECT * INTO NEW FROM integration.database_sources WHERE id = NEW.id;
        IF NOT FOUND THEN RETURN NULL; END IF;
        IF NEW.active_snapshot_id IS NOT NULL AND (
            NEW.status <> 'active'
            OR NOT EXISTS (
                SELECT 1
                FROM integration.schema_snapshots ss
                WHERE ss.id = NEW.active_snapshot_id
                  AND ss.database_source_id = NEW.id
                  AND ss.status = 'active'
            )
        ) THEN
            RAISE EXCEPTION 'invalid active database schema snapshot' USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'schema_snapshots' THEN
        SELECT * INTO NEW FROM integration.schema_snapshots WHERE id = NEW.id;
        IF NOT FOUND THEN RETURN NULL; END IF;
        IF NEW.status = 'active' AND NOT EXISTS (
            SELECT 1
            FROM integration.database_sources ds
            WHERE ds.id = NEW.database_source_id
              AND ds.status = 'active'
              AND ds.active_snapshot_id = NEW.id
        ) THEN
            RAISE EXCEPTION 'active schema snapshot is not the source pointer'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.status <> 'active' AND EXISTS (
            SELECT 1
            FROM integration.database_sources ds
            WHERE ds.id = NEW.database_source_id
              AND ds.active_snapshot_id = NEW.id
        ) THEN
            RAISE EXCEPTION 'source pointer must reference an active schema snapshot'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION integration.validate_database_source_snapshot() FROM PUBLIC;
DROP TRIGGER IF EXISTS validate_database_source_snapshot ON integration.database_sources;
CREATE CONSTRAINT TRIGGER validate_database_source_snapshot
AFTER INSERT OR UPDATE ON integration.database_sources
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION integration.validate_database_source_snapshot();
DROP TRIGGER IF EXISTS validate_schema_snapshot_source_pointer ON integration.schema_snapshots;
CREATE CONSTRAINT TRIGGER validate_schema_snapshot_source_pointer
AFTER INSERT OR UPDATE ON integration.schema_snapshots
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION integration.validate_database_source_snapshot();
"""
    )


def downgrade() -> None:
    raise RuntimeError("Rollback requires an explicit data recovery plan.")
