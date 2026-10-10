# Backup and independent restore

`deploy/backup_restore.py` is the bounded recovery helper for the EnterpriseMind
deployment workflow. It is intentionally separate from the normal
application start command. The helper never connects to the Database MCP source
database or issues `DROP`, `TRUNCATE`, `down -v`, or overwrite operations.

## Required resource identity

Before a backup, verify the source marker was produced by the current candidate
and that its Compose project, ports, migration, fixture, Redis, MinIO and MCP
checks are true. The marker is passed with `--source-identity`; a source project
outside `em-p10-*` is rejected. The backup output must be a new empty directory.

Before a restore, make a new target project in the `em-p10-restore-*` namespace,
with new PostgreSQL/MinIO/Redis data volumes and non-overlapping loopback ports.
The restore target keeps the source bucket name because `infra.storage_objects`
stores that name; the target MinIO endpoint and volume must be independent, and
the bucket with that name must be absent there before restore.
Write an identity marker containing at least:

```json
{
  "project": "em-p10-restore-20260922-a",
  "identity_verified": true,
  "target_data_is_new": true,
  "resource_scope": "EnterpriseMind-only"
}
```

The target secret directory is provisioned separately and is never copied from a
backup. A new token signing key is mandatory. Secret values are read from files,
and only names and SHA-256 fingerprints are recorded in the backup manifest.

For an isolated Compose target, use
`compose.restore.override.yaml` only with a fresh project name and ports. First
run read-only checks for existing containers and volumes with the exact project
label; if anything is already present, choose a new project suffix. Start the
data services with `deploy/compose.yaml` plus this override and capture the
container IDs, image IDs, loopback mappings, `PGDATA` path and volume labels.
Precreate the named database roles with the source role flags and required
extensions, while leaving all application schemas absent for `pg_restore`.
Do not run application migrations before restore. Never use `down -v` or a
volume name from another project.

## Commands

Run with the repository's global Python environment that has `psycopg`, `redis`,
and `minio`, plus PostgreSQL client tools on `PATH`. Set `--pg-dump-bin` and
`--pg-restore-bin` when the installed client executables have versioned names.

```text
python deploy/backup_restore.py backup --run-id 20260922-source-a \
  --source-project em-p10-20260921-lm01 \
  --source-identity .local/phase10/<run>/runtime/process-resources-controlled.json \
  --source-docker-bin "wsl -d Ubuntu -- docker" \
  --write-freeze-ack P10-10-SOURCE-WRITES-FROZEN \
  --output .local/phase10/<run>/restore/backup-20260922-source-a \
  --secret-dir <controlled-secret-directory> \
  --pg-dump-bin pg_dump18.6 \
  --pg-port <source-app-pg-port> --pg-database <source-db> --pg-user <source-owner> \
  --pg-password-file <source-pg-password-file> \
  --minio-endpoint <source-minio-endpoint> \
  --minio-access-file <source-minio-access-file> \
  --minio-secret-file <source-minio-secret-file> \
  --minio-bucket <source-bucket> \
  --redis-port <source-redis-port> --redis-password-file <source-redis-password-file> \
  --redis-stream <source-stream>

python deploy/backup_restore.py restore --dry-run \
  --backup <backup-directory> \
  --target-project em-p10-restore-20260922-a \
  --target-identity <new-target-identity-marker> \
  --target-secret-dir <new-target-secret-directory> \
  --pg-restore-bin pg_restore18.6 \
  --target-pg-port <new-pg-port> --target-pg-database <new-db> --target-pg-user <new-owner> \
  --target-minio-endpoint <new-minio-endpoint> --target-minio-bucket <source-bucket-name> \
  --target-redis-port <new-redis-port> \
  --evidence <restore-evidence.json>

python deploy/backup_restore.py restore --allow-restore ...
```

The backup command also requires a coordinated write-freeze acknowledgement and
rechecks the live Docker project labels, loopback mappings and volume labels;
the historical marker alone cannot authorize a source snapshot. Both restore
modes verify every manifest-listed backup file's path, size and SHA-256, inspect
the PostgreSQL archive, query target Docker identities, check target role names
and flags, require the target MinIO bucket to be absent, and require an empty
target Redis database. PostgreSQL volume identity is derived from the inspected
container's `PGDATA`; the matching most-specific mount must be a project-labeled
volume. Dry run also checks target Secrets and token-key rotation, then records
the read-only checks without writing target data. Actual writes additionally
require `--allow-restore`.

PostgreSQL uses `pg_restore --exit-on-error --no-owner` and preserves the
captured table ACLs. Required roles must already exist with the same names and
non-secret flags; the restore then checks each captured grant, including
`TRUNCATE` and PostgreSQL 18 `MAINTAIN`. MinIO validates every uploaded object's
size and SHA-256; Redis is deliberately rebuilt empty. The target secret
directory should use `redis_app_password`; `redis_password` remains accepted
for existing drill directories.
The identity-sequence check inserts a uniquely named department inside a
savepoint and rolls the row back. The sequence advances, but no probe business
row remains, including when the source is itself a previously restored database.
Queue/cache contents are not replayed, and checkpoint rows are data only. A
restored worker still needs current authorization and confirmation, and an
in-process `DatabaseExportPlan` must be regenerated.

## Manifest and recovery preparation

`backup-manifest.json` records migration, extension, role/grant metadata,
sequence values, table counts, object key/version/size/MIME/hash, Redis queue
policy, release-material hashes and the snapshot boundary. It contains no
passwords or token signing keys. `backup-failure.json` and restore evidence
record status, whether target writes started, and redacted error types only.

For production recovery, prepare an approved Linux host, target credentials,
an independent target volume, an operator-reviewed restore procedure and
RPO/RTO measurements. Provision the named target secrets through the approved
secret manager or file handoff, rotate the token signing key, and record the
handoff separately. The manifest stores metadata and fingerprints; password
and token-key material must be supplied through the separate secret workflow.
