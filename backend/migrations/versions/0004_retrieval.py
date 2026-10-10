"""Anchored chunks and model-specific vector storage, without invented model dimensions."""

from alembic import op

revision = "0004_retrieval"
down_revision = "0003_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE knowledge.chunks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    revision_id bigint NOT NULL,
    document_id bigint NOT NULL,
    knowledge_base_id bigint NOT NULL,
    ordinal integer NOT NULL,
    chunk_type varchar(30) NOT NULL,
    content text NOT NULL,
    search_tokens text NOT NULL,
    search_vector tsvector NOT NULL,
    token_count integer NOT NULL,
    context_group varchar(100),
    status varchar(20) NOT NULL DEFAULT 'pending',
    metadata jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_chunks_ordinal UNIQUE (revision_id, ordinal),
    CONSTRAINT ck_chunks_counts CHECK (ordinal >= 0 AND token_count > 0),
    CONSTRAINT ck_chunks_type CHECK (chunk_type IN ('text','table','code','image','mixed')),
    CONSTRAINT ck_chunks_status CHECK (status IN ('pending','ready','failed','retired')),
    CONSTRAINT ck_chunks_text CHECK (length(content) > 0 AND length(search_tokens) > 0),
    FOREIGN KEY (revision_id, document_id)
        REFERENCES knowledge.document_revisions (id, document_id) ON DELETE CASCADE,
    FOREIGN KEY (document_id, knowledge_base_id)
        REFERENCES knowledge.documents (id, knowledge_base_id) ON DELETE CASCADE
);
CREATE TABLE knowledge.chunk_anchors (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chunk_id bigint NOT NULL REFERENCES knowledge.chunks(id) ON DELETE CASCADE,
    anchor_id bigint NOT NULL REFERENCES knowledge.source_anchors(id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    content_start integer NOT NULL,
    content_end integer NOT NULL,
    quote_text text NOT NULL,
    CONSTRAINT uq_chunk_anchors_anchor UNIQUE (chunk_id, anchor_id),
    CONSTRAINT uq_chunk_anchors_ordinal UNIQUE (chunk_id, ordinal),
    CONSTRAINT ck_chunk_anchors_range CHECK
        (ordinal >= 0 AND content_start >= 0 AND content_end > content_start)
);
CREATE TABLE knowledge.chunk_embeddings (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chunk_id bigint NOT NULL REFERENCES knowledge.chunks(id) ON DELETE CASCADE,
    model_key varchar(200) NOT NULL,
    dimensions smallint NOT NULL,
    embedding vector NOT NULL,
    status varchar(20) NOT NULL,
    error_code varchar(100),
    embedded_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_chunk_embeddings_model UNIQUE (chunk_id, model_key),
    CONSTRAINT ck_embeddings_dimensions CHECK
        (dimensions > 0 AND vector_dims(embedding) = dimensions AND vector_norm(embedding) > 0),
    CONSTRAINT ck_embeddings_status CHECK (status IN ('ready','failed','retired'))
);
CREATE INDEX ix_chunks_search ON knowledge.chunks USING gin(search_vector) WHERE status='ready';
CREATE INDEX ix_chunk_anchors_anchor ON knowledge.chunk_anchors(anchor_id);
CREATE INDEX ix_source_anchors_source ON knowledge.source_anchors(source_unit_id, id);
CREATE INDEX ix_documents_name_trgm ON knowledge.documents USING gin(name_key gin_trgm_ops)
    WHERE deleted_at IS NULL;
CREATE INDEX ix_documents_description_trgm ON knowledge.documents
    USING gin(description gin_trgm_ops)
    WHERE deleted_at IS NULL AND description IS NOT NULL;
CREATE INDEX ix_folders_name_trgm ON knowledge.folders USING gin(name_key gin_trgm_ops)
    WHERE deleted_at IS NULL;
""")
    op.execute("""
CREATE FUNCTION knowledge.validate_chunk_anchor() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, knowledge AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM knowledge.chunk_anchors ca
        JOIN knowledge.chunks c ON c.id=ca.chunk_id
        JOIN knowledge.source_anchors a ON a.id=ca.anchor_id
        WHERE ca.id=NEW.id AND (
            c.revision_id <> a.revision_id OR ca.content_end > char_length(c.content)
            OR substring(c.content FROM ca.content_start + 1
                         FOR ca.content_end - ca.content_start) <> ca.quote_text
            OR ca.quote_text = '' OR a.source_text IS NULL
            OR position(ca.quote_text IN a.source_text) = 0
        )
    ) THEN
        RAISE EXCEPTION 'invalid chunk source mapping' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION knowledge.validate_chunk_anchor() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER ck_chunk_anchor_same_revision
AFTER INSERT OR UPDATE ON knowledge.chunk_anchors DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION knowledge.validate_chunk_anchor();

CREATE FUNCTION knowledge.protect_indexed_source() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, knowledge AS $$
BEGIN
    IF TG_TABLE_NAME = 'chunks' THEN
        IF EXISTS (SELECT 1 FROM knowledge.chunk_anchors WHERE chunk_id=OLD.id)
           AND (OLD.revision_id, OLD.document_id, OLD.knowledge_base_id, OLD.content, OLD.ordinal)
             IS DISTINCT FROM
               (NEW.revision_id, NEW.document_id, NEW.knowledge_base_id, NEW.content, NEW.ordinal)
        THEN RAISE EXCEPTION 'indexed chunk is immutable' USING ERRCODE='23514';
        END IF;
    ELSE
        IF EXISTS (SELECT 1 FROM knowledge.chunk_anchors WHERE anchor_id=OLD.id)
           AND (OLD.revision_id, OLD.source_unit_id, OLD.locator, OLD.source_text)
             IS DISTINCT FROM (NEW.revision_id, NEW.source_unit_id, NEW.locator, NEW.source_text)
        THEN RAISE EXCEPTION 'indexed anchor is immutable' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION knowledge.protect_indexed_source() FROM PUBLIC;
CREATE TRIGGER protect_chunk BEFORE UPDATE ON knowledge.chunks
FOR EACH ROW EXECUTE FUNCTION knowledge.protect_indexed_source();
CREATE TRIGGER protect_anchor BEFORE UPDATE ON knowledge.source_anchors
FOR EACH ROW EXECUTE FUNCTION knowledge.protect_indexed_source();
""")


def downgrade() -> None:
    raise RuntimeError("Rollback requires an explicit data recovery plan.")
