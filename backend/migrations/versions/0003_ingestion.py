"""Document upload, durable dispatch and basic parsing storage."""

from alembic import op

revision = "0003_ingestion"
down_revision = "0002_organization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE infra.outbox_events (
	id BIGINT GENERATED ALWAYS AS IDENTITY,
	aggregate_type VARCHAR(50) NOT NULL,
	aggregate_id BIGINT NOT NULL,
	event_type VARCHAR(100) NOT NULL,
	payload JSONB NOT NULL,
	deduplication_key VARCHAR(300) NOT NULL,
	status VARCHAR(20) DEFAULT 'pending' NOT NULL,
	attempt_count INTEGER DEFAULT 0 NOT NULL,
	next_attempt_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	lease_owner VARCHAR(200),
	lease_expires_at TIMESTAMP WITH TIME ZONE,
	last_error TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	published_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_outbox_deduplication UNIQUE (deduplication_key),
	CONSTRAINT ck_outbox_status CHECK (status IN ('pending','publishing','published','failed')),
        CONSTRAINT ck_outbox_values CHECK (attempt_count >= 0 AND (status <> 'published' OR
    published_at IS NOT NULL))
)
""")
    op.execute("""
CREATE INDEX ix_outbox_dispatch ON infra.outbox_events (status, next_attempt_at)
""")
    op.execute("""
CREATE TABLE infra.storage_objects (
	id BIGINT GENERATED ALWAYS AS IDENTITY,
	bucket_name VARCHAR(100) NOT NULL,
	object_key VARCHAR(1000) NOT NULL,
	object_type VARCHAR(30) NOT NULL,
	size_bytes BIGINT NOT NULL,
	content_type VARCHAR(200) NOT NULL,
	content_sha256 CHAR(64),
	status VARCHAR(20) NOT NULL,
	retention_until TIMESTAMP WITH TIME ZONE,
	last_error TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_storage_objects_key UNIQUE (bucket_name, object_key),
        CONSTRAINT ck_storage_objects_values CHECK (size_bytes >= 0 AND length(bucket_name) > 0
    AND length(object_key) > 0),
        CONSTRAINT ck_storage_objects_type CHECK (object_type IN
    ('original','preview','derived','generated','export')),
        CONSTRAINT ck_storage_objects_status CHECK (status IN
    ('staged','available','pending_delete','deleting','deleted','error'))
)
""")
    op.execute("""
CREATE TABLE knowledge.folders (
	id BIGINT GENERATED ALWAYS AS IDENTITY,
	knowledge_base_id BIGINT NOT NULL,
	parent_id BIGINT,
	name VARCHAR(255) NOT NULL,
	name_key VARCHAR(255) NOT NULL,
	row_version BIGINT DEFAULT 1 NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	purge_after TIMESTAMP WITH TIME ZONE,
	deletion_batch_id BIGINT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_folders_id_base UNIQUE (id, knowledge_base_id),
        FOREIGN KEY(parent_id, knowledge_base_id) REFERENCES knowledge.folders (id,
    knowledge_base_id) ON DELETE RESTRICT,
	CONSTRAINT ck_folders_name_version CHECK (row_version >= 1 AND length(name_key) > 0),
        CONSTRAINT ck_folders_deletion CHECK ((deleted_at IS NULL AND purge_after IS NULL AND
    deletion_batch_id IS NULL) OR (deleted_at IS NOT NULL AND purge_after IS NOT NULL AND
    deletion_batch_id IS NOT NULL AND purge_after > deleted_at)),
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge.knowledge_bases (id) ON DELETE RESTRICT,
	FOREIGN KEY(deletion_batch_id) REFERENCES knowledge.deletion_batches (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_folders_child_name ON knowledge.folders (knowledge_base_id, parent_id,
    name_key) WHERE parent_id IS NOT NULL AND deleted_at IS NULL
""")
    op.execute("""
CREATE UNIQUE INDEX uq_folders_root_name ON knowledge.folders (knowledge_base_id, name_key)
    WHERE parent_id IS NULL AND deleted_at IS NULL
""")
    op.execute("""
CREATE TABLE knowledge.documents (
	id BIGINT GENERATED ALWAYS AS IDENTITY,
	knowledge_base_id BIGINT NOT NULL,
	folder_id BIGINT,
	name VARCHAR(255) NOT NULL,
	name_key VARCHAR(255) NOT NULL,
	description TEXT,
	created_by BIGINT NOT NULL,
	processing_status VARCHAR(30) DEFAULT 'pending' NOT NULL,
	latest_revision_no INTEGER DEFAULT 1 NOT NULL,
	active_revision_id BIGINT,
	row_version BIGINT DEFAULT 1 NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	purge_after TIMESTAMP WITH TIME ZONE,
	deletion_batch_id BIGINT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_documents_id_base UNIQUE (id, knowledge_base_id),
        FOREIGN KEY(folder_id, knowledge_base_id) REFERENCES knowledge.folders (id,
    knowledge_base_id) ON DELETE RESTRICT,
        CONSTRAINT ck_documents_status CHECK (processing_status IN
    ('pending','processing','success','partial_success','failed')),
        CONSTRAINT ck_documents_name_version CHECK (row_version >= 1 AND latest_revision_no >= 1
    AND length(name_key) > 0),
        CONSTRAINT ck_documents_deletion CHECK ((deleted_at IS NULL AND purge_after IS NULL AND
    deletion_batch_id IS NULL) OR (deleted_at IS NOT NULL AND purge_after IS NOT NULL AND
    deletion_batch_id IS NOT NULL AND purge_after > deleted_at)),
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge.knowledge_bases (id) ON DELETE RESTRICT,
	FOREIGN KEY(created_by) REFERENCES iam.users (id) ON DELETE RESTRICT,
	FOREIGN KEY(deletion_batch_id) REFERENCES knowledge.deletion_batches (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_documents_folder_name ON knowledge.documents (knowledge_base_id,
    folder_id, name_key) WHERE folder_id IS NOT NULL AND deleted_at IS NULL
""")
    op.execute("""
CREATE UNIQUE INDEX uq_documents_root_name ON knowledge.documents (knowledge_base_id, name_key)
    WHERE folder_id IS NULL AND deleted_at IS NULL
""")
    op.execute("""
CREATE TABLE knowledge.document_revisions (
	id BIGINT GENERATED ALWAYS AS IDENTITY,
	document_id BIGINT NOT NULL,
	revision_no INTEGER NOT NULL,
	trigger_type VARCHAR(30) NOT NULL,
	original_object_id BIGINT NOT NULL,
	original_file_name VARCHAR(500) NOT NULL,
	file_extension VARCHAR(20) NOT NULL,
	mime_type VARCHAR(200) NOT NULL,
	size_bytes BIGINT NOT NULL,
	content_sha256 CHAR(64) NOT NULL,
	processing_status VARCHAR(30) DEFAULT 'pending' NOT NULL,
	initiated_by BIGINT NOT NULL,
	activated_at TIMESTAMP WITH TIME ZONE,
	retired_at TIMESTAMP WITH TIME ZONE,
	failure_summary JSONB,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_revisions_document_no UNIQUE (document_id, revision_no),
	CONSTRAINT uq_revisions_id_document UNIQUE (id, document_id),
	CONSTRAINT uq_revisions_original_object UNIQUE (original_object_id),
	CONSTRAINT ck_revisions_values CHECK (revision_no >= 1 AND size_bytes >= 0),
        CONSTRAINT ck_revisions_trigger CHECK (trigger_type IN
    ('initial_upload','overwrite','generated_write')),
        CONSTRAINT ck_revisions_status CHECK (processing_status IN
    ('pending','processing','success','partial_success','failed')),
	FOREIGN KEY(document_id) REFERENCES knowledge.documents (id) ON DELETE CASCADE,
	FOREIGN KEY(original_object_id) REFERENCES infra.storage_objects (id) ON DELETE RESTRICT,
	FOREIGN KEY(initiated_by) REFERENCES iam.users (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_revisions_unfinished ON knowledge.document_revisions (document_id) WHERE
    processing_status IN ('pending','processing')
""")
    op.execute("""
CREATE TABLE knowledge.processing_tasks (
	id BIGINT GENERATED ALWAYS AS IDENTITY,
	revision_id BIGINT NOT NULL,
	task_type VARCHAR(20) NOT NULL,
	retry_of_task_id BIGINT,
	status VARCHAR(30) DEFAULT 'pending' NOT NULL,
	current_stage VARCHAR(30),
	stage_current INTEGER,
	stage_total INTEGER,
	error_code VARCHAR(100),
	error_message TEXT,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_tasks_id_revision UNIQUE (id, revision_id),
	CONSTRAINT ck_tasks_type CHECK (task_type IN ('full','retry_failed')),
        CONSTRAINT ck_tasks_status CHECK (status IN
    ('pending','processing','success','partial_success','failed')),
        CONSTRAINT ck_tasks_stage CHECK (current_stage IS NULL OR current_stage IN
    ('parse','ocr','vision','table','code','chunk','embedding','keyword_index','preview','activate')),
        CONSTRAINT ck_tasks_progress CHECK ((stage_current IS NULL AND stage_total IS NULL) OR
    (stage_current IS NOT NULL AND stage_total IS NOT NULL AND stage_current >= 0 AND
    stage_total >= stage_current)),
	FOREIGN KEY(revision_id) REFERENCES knowledge.document_revisions (id) ON DELETE CASCADE,
	FOREIGN KEY(retry_of_task_id) REFERENCES knowledge.processing_tasks (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_tasks_unfinished ON knowledge.processing_tasks (revision_id) WHERE status
    IN ('pending','processing')
""")
    op.execute("""
CREATE TABLE knowledge.source_units (
	id BIGINT GENERATED ALWAYS AS IDENTITY,
	revision_id BIGINT NOT NULL,
	unit_type VARCHAR(20) NOT NULL,
	ordinal INTEGER NOT NULL,
	label VARCHAR(255),
	width NUMERIC,
	height NUMERIC,
	extraction_status VARCHAR(20) NOT NULL,
	extracted_text TEXT,
	metadata JSONB,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_source_units_order UNIQUE (revision_id, unit_type, ordinal),
	CONSTRAINT uq_source_units_id_revision UNIQUE (id, revision_id),
        CONSTRAINT ck_source_units_type CHECK (unit_type IN
    ('document','page','slide','sheet','image')),
	CONSTRAINT ck_source_units_ordinal CHECK (ordinal >= 0),
        CONSTRAINT ck_source_units_size CHECK ((width IS NULL AND height IS NULL) OR (width IS
    NOT NULL AND height IS NOT NULL AND width > 0 AND height > 0)),
        CONSTRAINT ck_source_units_status CHECK (extraction_status IN
    ('pending','success','failed','skipped')),
	FOREIGN KEY(revision_id) REFERENCES knowledge.document_revisions (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE TABLE knowledge.content_elements (
	id BIGINT GENERATED ALWAYS AS IDENTITY,
	source_unit_id BIGINT NOT NULL,
	parent_element_id BIGINT,
	element_type VARCHAR(30) NOT NULL,
	ordinal INTEGER NOT NULL,
	content_text TEXT,
	structured_payload JSONB,
	token_estimate INTEGER,
	status VARCHAR(20) DEFAULT 'success' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_elements_order UNIQUE (source_unit_id, ordinal),
	CONSTRAINT uq_elements_id_source UNIQUE (id, source_unit_id),
        FOREIGN KEY(parent_element_id, source_unit_id) REFERENCES knowledge.content_elements
    (id, source_unit_id) ON DELETE CASCADE,
        CONSTRAINT ck_elements_values CHECK (ordinal >= 0 AND (token_estimate IS NULL OR
    token_estimate >= 0)),
	CONSTRAINT ck_elements_status CHECK (status IN ('success','failed')),
	FOREIGN KEY(source_unit_id) REFERENCES knowledge.source_units (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE TABLE knowledge.processing_units (
	id BIGINT GENERATED ALWAYS AS IDENTITY,
	revision_id BIGINT NOT NULL,
	stage VARCHAR(30) NOT NULL,
	unit_key VARCHAR(200) NOT NULL,
	source_unit_id BIGINT,
	latest_task_id BIGINT NOT NULL,
	status VARCHAR(20) DEFAULT 'pending' NOT NULL,
	attempt_count INTEGER DEFAULT 0 NOT NULL,
	current INTEGER,
	total INTEGER,
	error_code VARCHAR(100),
	error_message TEXT,
	lease_owner VARCHAR(200),
	lease_expires_at TIMESTAMP WITH TIME ZONE,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_processing_units_key UNIQUE (revision_id, stage, unit_key),
        FOREIGN KEY(latest_task_id, revision_id) REFERENCES knowledge.processing_tasks (id,
    revision_id) DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY(source_unit_id, revision_id) REFERENCES knowledge.source_units (id,
    revision_id) ON DELETE SET NULL (source_unit_id),
        CONSTRAINT ck_processing_units_stage CHECK (stage IN
    ('parse','ocr','vision','table','code','chunk','embedding','keyword_index','preview','activate')),
        CONSTRAINT ck_processing_units_status CHECK (status IN
    ('pending','processing','success','failed','skipped')),
	CONSTRAINT ck_processing_units_attempts CHECK (attempt_count >= 0),
        CONSTRAINT ck_processing_units_progress CHECK ((current IS NULL AND total IS NULL) OR
    (current IS NOT NULL AND total IS NOT NULL AND current >= 0 AND total >= current)),
        CONSTRAINT ck_processing_units_lease CHECK ((status = 'processing' AND lease_owner IS
    NOT NULL AND lease_expires_at IS NOT NULL) OR (status <> 'processing' AND lease_owner IS
    NULL AND lease_expires_at IS NULL)),
	FOREIGN KEY(revision_id) REFERENCES knowledge.document_revisions (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_processing_units_pending ON knowledge.processing_units (status, stage,
    lease_expires_at)
""")
    op.execute("""
CREATE TABLE knowledge.source_anchors (
	id BIGINT GENERATED ALWAYS AS IDENTITY,
	revision_id BIGINT NOT NULL,
	source_unit_id BIGINT NOT NULL,
	element_id BIGINT,
	locator_type VARCHAR(20) NOT NULL,
	locator JSONB NOT NULL,
	source_text TEXT,
	source_text_sha256 CHAR(64),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
        FOREIGN KEY(source_unit_id, revision_id) REFERENCES knowledge.source_units (id,
    revision_id) ON DELETE CASCADE,
        FOREIGN KEY(element_id, source_unit_id) REFERENCES knowledge.content_elements (id,
    source_unit_id) ON DELETE SET NULL (element_id),
        CONSTRAINT ck_anchors_type CHECK (locator_type IN
    ('pdf','image','docx','pptx','xlsx','text')),
	FOREIGN KEY(revision_id) REFERENCES knowledge.document_revisions (id) ON DELETE CASCADE
)
""")
    op.execute("""
ALTER TABLE knowledge.documents ADD CONSTRAINT fk_documents_active_revision FOREIGN
    KEY(active_revision_id, id) REFERENCES knowledge.document_revisions (id, document_id)
    DEFERRABLE INITIALLY DEFERRED
""")


def downgrade() -> None:
    raise RuntimeError("Rollback requires an explicit data recovery plan.")
