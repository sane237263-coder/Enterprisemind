"""Bind staged originals to their owning conversation without an attachment table."""

from alembic import op

revision = "0007_attachments"
down_revision = "0006_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
ALTER TABLE infra.storage_objects
    ADD COLUMN attachment_conversation_id BIGINT,
    ADD COLUMN attachment_file_name VARCHAR(500),
    ADD COLUMN attachment_file_extension VARCHAR(20),
    ADD CONSTRAINT fk_storage_attachment_conversation
        FOREIGN KEY (attachment_conversation_id) REFERENCES agent.conversations (id)
        ON DELETE SET NULL,
    ADD CONSTRAINT ck_storage_attachment_metadata CHECK (
        (attachment_file_name IS NULL AND attachment_file_extension IS NULL
            AND attachment_conversation_id IS NULL)
        OR (attachment_file_name IS NOT NULL AND length(attachment_file_name) > 0
            AND attachment_file_extension IS NOT NULL
            AND length(attachment_file_extension) > 0 AND object_type = 'original')
    );
CREATE INDEX ix_storage_attachment_conversation
    ON infra.storage_objects (attachment_conversation_id, id)
    WHERE attachment_conversation_id IS NOT NULL;
    """)


def downgrade() -> None:
    raise RuntimeError("Rollback requires an explicit data recovery plan.")
