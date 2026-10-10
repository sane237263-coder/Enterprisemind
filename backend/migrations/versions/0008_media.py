"""Register revision-bound previews and extraction artifacts."""

from alembic import op

revision = "0008_media"
down_revision = "0007_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE knowledge.document_artifacts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    revision_id BIGINT NOT NULL REFERENCES knowledge.document_revisions(id) ON DELETE CASCADE,
    source_unit_id BIGINT,
    artifact_type VARCHAR(40) NOT NULL,
    storage_object_id BIGINT NOT NULL UNIQUE
        REFERENCES infra.storage_objects(id) ON DELETE RESTRICT,
    status VARCHAR(20) NOT NULL DEFAULT 'ready',
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_artifacts_source FOREIGN KEY (source_unit_id, revision_id)
        REFERENCES knowledge.source_units(id, revision_id) ON DELETE CASCADE,
    CONSTRAINT ck_artifacts_status CHECK (status IN ('pending','ready','failed')),
    CONSTRAINT uq_artifacts_output UNIQUE NULLS NOT DISTINCT
        (revision_id, source_unit_id, artifact_type)
);
CREATE INDEX ix_artifacts_revision ON knowledge.document_artifacts(revision_id);
    """)


def downgrade() -> None:
    raise RuntimeError("Rollback requires an explicit data recovery plan.")
