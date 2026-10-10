"""Bind database schema policies to external object identity and config epochs."""

from alembic import op

revision = "0011_database_identity"
down_revision = "0010_database"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0010 may already be present in an application database.  The new
    # identity fields are nullable for physical migration compatibility, but
    # an old active snapshot must not remain queryable: its same-name objects
    # have never been bound to an external OID/signature.  Retire it after
    # adding the fields, preserving the snapshot as audit/history data until a
    # complete refresh proves continuity.
    op.execute(
        """
ALTER TABLE integration.database_sources
    ADD COLUMN configuration_version BIGINT NOT NULL DEFAULT 1;
ALTER TABLE integration.database_sources
    ADD CONSTRAINT ck_database_sources_configuration_version
        CHECK (configuration_version >= 1);

ALTER TABLE integration.schema_relations
    ADD COLUMN relation_oid BIGINT,
    ADD COLUMN relation_signature VARCHAR(128),
    ADD COLUMN view_definition_signature VARCHAR(128);
ALTER TABLE integration.schema_relations
    ADD CONSTRAINT ck_schema_relations_oid
        CHECK (relation_oid IS NULL OR relation_oid >= 0);
CREATE UNIQUE INDEX uq_schema_relations_snapshot_oid
    ON integration.schema_relations (snapshot_id, relation_oid)
    WHERE relation_oid IS NOT NULL;

ALTER TABLE integration.schema_columns
    ADD COLUMN is_nullable BOOLEAN;

ALTER TABLE integration.schema_constraints
    DROP CONSTRAINT IF EXISTS ck_schema_constraints_type;
ALTER TABLE integration.schema_constraints
    ADD CONSTRAINT ck_schema_constraints_type
        CHECK (constraint_type IN ('primary_key', 'unique', 'foreign_key'));
ALTER TABLE integration.schema_constraints
    DROP CONSTRAINT IF EXISTS ck_schema_constraints_reference;
ALTER TABLE integration.schema_constraints
    ADD CONSTRAINT ck_schema_constraints_reference CHECK (
        (constraint_type IN ('primary_key', 'unique')
            AND referenced_schema_name IS NULL
            AND referenced_relation_name IS NULL
            AND referenced_column_names IS NULL)
        OR
        (constraint_type = 'foreign_key'
            AND referenced_schema_name IS NOT NULL
            AND referenced_relation_name IS NOT NULL
            AND referenced_column_names IS NOT NULL
            AND cardinality(referenced_column_names) = cardinality(column_names))
    );

UPDATE integration.schema_snapshots AS snapshot
SET status = 'retired',
    error_code = 'SCHEMA_REFRESH_REQUIRED',
    error_message = 'Snapshot predates external object identity metadata; refresh required.'
WHERE snapshot.status = 'active'
  AND EXISTS (
      SELECT 1
      FROM integration.schema_relations AS relation
      WHERE relation.snapshot_id = snapshot.id
        AND (
            relation.relation_oid IS NULL
            OR relation.relation_signature IS NULL
            OR (
                relation.relation_type = 'view'
                AND relation.view_definition_signature IS NULL
            )
        )
  );

UPDATE integration.database_sources AS source
SET active_snapshot_id = NULL
WHERE source.active_snapshot_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM integration.schema_snapshots AS snapshot
      WHERE snapshot.id = source.active_snapshot_id
        AND snapshot.status = 'active'
  );
"""
    )


def downgrade() -> None:
    raise RuntimeError("Rollback requires an explicit data recovery plan.")
