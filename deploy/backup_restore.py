"""Bounded EnterpriseMind backup and independent restore helper.

The command is deliberately conservative:

* ``backup`` refuses a non-verified source marker or a non-empty destination;
* ``restore --dry-run`` verifies backup integrity and performs read-only target checks;
* an actual restore requires ``--allow-restore`` and an explicitly marked,
  empty target PostgreSQL, MinIO bucket boundary and Redis database;
* no command contains a password, and no command can drop/truncate a target;
* Redis queue/cache state is rebuilt and the target token signing key must be
  newly provisioned, so an old revoked token cannot become valid by restoring
  a cache snapshot.

It is intended for the project's isolated acceptance environment and an
approved deployment host.  It never backs up a connected enterprise source
database configured for Database MCP.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

try:
    import psycopg
    import redis
    from minio import Minio
except ImportError as error:  # pragma: no cover - target runtime supplies these
    raise SystemExit("backup_restore.py requires psycopg, redis and minio") from error


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_SCHEMAS = (
    "iam",
    "knowledge",
    "agent",
    "integration",
    "infra",
    "langgraph_checkpoint",
)
CORE_TABLES = (
    "iam.users",
    "iam.departments",
    "knowledge.knowledge_bases",
    "knowledge.documents",
    "knowledge.document_revisions",
    "knowledge.processing_tasks",
    "knowledge.document_artifacts",
    "knowledge.chunks",
    "knowledge.chunk_embeddings",
    "infra.storage_objects",
    "infra.outbox_events",
    "agent.conversations",
    "agent.messages",
    "agent.agent_runs",
    "agent.answer_sources",
    "agent.pending_actions",
    "langgraph_checkpoint.checkpoints",
    "langgraph_checkpoint.checkpoint_blobs",
)
SECRET_NAMES = (
    "postgres_owner_password",
    "postgres_app_password",
    "minio_app_access",
    "minio_app_secret",
    "redis_app_password",
    "redis_password",
    "token_signing_key",
)


class BackupError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_secret(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError) as error:
        raise BackupError(f"cannot read Secret file: {path.name}") from error
    if not value or any(char in value for char in "\x00\r\n"):
        raise BackupError(f"Secret is not a single non-empty line: {path.name}")
    return value


def redis_secret_file(secret_dir: Path) -> Path:
    """Prefer the deployment's canonical name while accepting older drill dirs."""

    for name in ("redis_app_password", "redis_password"):
        path = secret_dir / name
        if path.is_file():
            return path
    raise BackupError("target Secret is missing: redis_app_password")


def require_project(value: str, *, kind: str) -> str:
    pattern = (
        r"em-p10-[a-z0-9][a-z0-9-]{2,62}"
        if kind == "source"
        else r"em-p10-restore-[a-z0-9][a-z0-9-]{2,62}"
    )
    if not re.fullmatch(pattern, value):
        raise BackupError(
            f"{kind} project identity is outside the EnterpriseMind acceptance namespace"
        )
    return value


def require_run_id(value: str) -> str:
    if not re.fullmatch(r"[0-9]{8}-[a-z0-9][a-z0-9-]{2,60}", value):
        raise BackupError("run_id must be a dated, bounded identifier")
    return value


def ensure_empty_directory(path: Path, *, label: str) -> None:
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise BackupError(f"{label} must be a new empty directory: {path}")
    else:
        path.mkdir(parents=True)


def safe_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_checked(args: list[str], *, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
    if completed.returncode:
        # The child command is never printed.  Its stderr may contain a
        # connection string or driver detail and therefore does not enter the
        # ordinary evidence record.
        raise BackupError(
            f"command failed with exit code {completed.returncode}: {Path(args[0]).name}"
        )


def docker_command(binary: str, *arguments: str) -> list[str]:
    """Allow a direct Docker binary or an explicit WSL Docker prefix."""

    prefix = shlex.split(binary, posix=True)
    if not prefix:
        raise BackupError("Docker command prefix is empty")
    return [*prefix, *arguments]


def tool_command(binary: str, label: str, *arguments: str) -> list[str]:
    prefix = shlex.split(binary, posix=True)
    if not prefix:
        raise BackupError(f"{label} command prefix is empty")
    return [*prefix, *arguments]


def validate_backup_file(
    backup: Path,
    relative_path: Any,
    expected_size: Any,
    expected_sha256: Any,
    *,
    label: str,
) -> Path:
    """Resolve and verify one manifest-listed file without leaving the backup."""

    if not isinstance(relative_path, str) or not relative_path:
        raise BackupError(f"backup {label} path is missing")
    normalized = relative_path.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(relative_path)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    ):
        raise BackupError(f"backup {label} path is unsafe")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
        raise BackupError(f"backup {label} size is invalid")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise BackupError(f"backup {label} SHA-256 is invalid")

    root = backup.resolve()
    candidate = root.joinpath(*posix_path.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise BackupError(
            f"backup {label} path is missing or outside the backup directory"
        ) from error
    if not resolved.is_file():
        raise BackupError(f"backup {label} is not a regular file")
    if resolved.stat().st_size != expected_size:
        raise BackupError(f"backup {label} size differs from its manifest")
    if sha256_file(resolved) != expected_sha256:
        raise BackupError(f"backup {label} SHA-256 differs from its manifest")
    return resolved


def validate_backup_artifacts(backup: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Verify every file the restore would read before it contacts the target."""

    postgres = manifest.get("postgres")
    if not isinstance(postgres, dict):
        raise BackupError("backup manifest lacks PostgreSQL metadata")
    dump_record = postgres.get("dump")
    if not isinstance(dump_record, dict) or dump_record.get("format") != "custom":
        raise BackupError("backup manifest lacks a custom-format PostgreSQL dump")
    dump = validate_backup_file(
        backup,
        dump_record.get("path"),
        dump_record.get("size"),
        dump_record.get("sha256"),
        label="PostgreSQL dump",
    )

    role_record = postgres.get("roles_and_grants")
    if not isinstance(role_record, dict):
        raise BackupError("backup manifest lacks application role/grant metadata")
    roles_path = validate_backup_file(
        backup,
        role_record.get("path"),
        role_record.get("size"),
        role_record.get("sha256"),
        label="role/grant metadata",
    )
    try:
        role_file = json.loads(roles_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BackupError("backup role/grant metadata file is invalid") from error
    if not isinstance(role_file, dict):
        raise BackupError("backup role/grant metadata file must be an object")
    if role_file.get("roles_without_passwords") != role_record.get(
        "roles_without_passwords"
    ) or role_file.get("table_grants") != role_record.get("table_grants"):
        raise BackupError("backup role/grant metadata differs from its manifest")

    minio = manifest.get("minio")
    objects = minio.get("objects") if isinstance(minio, dict) else None
    if not isinstance(objects, list):
        raise BackupError("backup manifest lacks MinIO object metadata")
    object_files: list[tuple[dict[str, Any], Path]] = []
    seen_paths: set[str] = set()
    seen_keys: set[str] = set()
    for item in objects:
        if not isinstance(item, dict):
            raise BackupError("backup contains an invalid MinIO object entry")
        relative_path = item.get("relative_path")
        key = item.get("key")
        if not isinstance(key, str) or not key or key in seen_keys:
            raise BackupError("backup contains an invalid or duplicate MinIO object key")
        seen_keys.add(key)
        if not isinstance(relative_path, str) or relative_path in seen_paths:
            raise BackupError("backup contains a missing or duplicate MinIO object path")
        seen_paths.add(relative_path)
        object_file = validate_backup_file(
            backup,
            relative_path,
            item.get("size"),
            item.get("sha256"),
            label="MinIO object",
        )
        object_files.append((item, object_file))

    release_materials = manifest.get("release_materials")
    if not isinstance(release_materials, list):
        raise BackupError("backup manifest lacks release-material metadata")
    material_paths: set[str] = set()
    for item in release_materials:
        if not isinstance(item, dict):
            raise BackupError("backup contains an invalid release-material entry")
        relative_path = item.get("path")
        if not isinstance(relative_path, str) or relative_path in material_paths:
            raise BackupError("backup contains a missing or duplicate release-material path")
        material_paths.add(relative_path)
        validate_backup_file(
            backup,
            str(PurePosixPath("release-materials") / PurePosixPath(relative_path)),
            item.get("size"),
            item.get("sha256"),
            label="release material",
        )

    return {"dump": dump, "objects": object_files}


def with_pgpass(secret_file: Path, username: str, callback: Any) -> Any:
    password = read_secret(secret_file)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
    path = Path(handle.name)
    try:

        def escape_pgpass(value: str) -> str:
            return value.replace("\\", "\\\\").replace(":", "\\:")

        handle.write(f"*:*:*:{escape_pgpass(username)}:{escape_pgpass(password)}\n")
        handle.close()
        try:
            path.chmod(0o600)
        except OSError:
            pass
        env = dict(os.environ)
        env["PGPASSFILE"] = str(path)
        return callback(env)
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def connect_pg(host: str, port: int, database: str, username: str, secret_file: Path) -> Any:
    password = read_secret(secret_file)
    try:
        return psycopg.connect(
            host=host,
            port=port,
            dbname=database,
            user=username,
            password=password,
            connect_timeout=5,
        )
    except Exception as error:
        raise BackupError("PostgreSQL connection failed") from error


def verify_source_marker(path: Path, project: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BackupError("source identity marker cannot be read") from error
    if payload.get("project") != project or payload.get("identity_verified") is not True:
        raise BackupError("source identity marker is not verified for the requested project")
    checks = payload.get("checks", {})
    required = ("docker_target_identity", "migration_matches_head", "fixture_count")
    if any(checks.get(name) is not True for name in required):
        raise BackupError("source marker lacks required identity/migration/fixture checks")
    return payload


def postgres_snapshot(
    *,
    host: str,
    port: int,
    database: str,
    username: str,
    secret_file: Path,
    output: Path,
    pg_dump_bin: str = "pg_dump",
) -> dict[str, Any]:
    dump = output / "postgres" / "enterprise_mind.dump"
    dump.parent.mkdir()

    def dump_with_env(env: dict[str, str]) -> None:
        run_checked(
            tool_command(
                pg_dump_bin,
                "PostgreSQL dump",
                "--format=custom",
                "--file",
                str(dump),
                "--host",
                host,
                "--port",
                str(port),
                "--username",
                username,
                "--dbname",
                database,
                "--no-owner",
                "--verbose",
            ),
            env=env,
        )

    with_pgpass(secret_file, username, dump_with_env)
    with connect_pg(host, port, database, username, secret_file) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user, inet_server_port()")
            database_identity_row = cursor.fetchone()
            database_identity = {
                "database": database_identity_row[0],
                "user": database_identity_row[1],
                "server_port": database_identity_row[2],
            }
            cursor.execute(
                "SELECT extname, extversion FROM pg_extension "
                "WHERE extname IN ('vector') ORDER BY extname"
            )
            extensions = [{"name": row[0], "version": row[1]} for row in cursor.fetchall()]
            cursor.execute("SELECT version_num FROM infra.alembic_version")
            migrations = [row[0] for row in cursor.fetchall()]
            counts: dict[str, int] = {}
            for table in CORE_TABLES:
                schema, name = table.split(".", 1)
                cursor.execute(f'SELECT count(*) FROM "{schema}"."{name}"')
                counts[table] = int(cursor.fetchone()[0])
            cursor.execute(
                "SELECT schemaname, sequencename, last_value FROM pg_sequences "
                "WHERE schemaname = ANY(%s) ORDER BY schemaname, sequencename",
                (list(APP_SCHEMAS),),
            )
            sequences = [
                {"schema": row[0], "name": row[1], "last_value": row[2]}
                for row in cursor.fetchall()
            ]
            cursor.execute(
                "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                "rolbypassrls, rolinherit FROM pg_roles "
                "WHERE rolname = %s OR rolname LIKE %s OR rolname LIKE %s "
                "ORDER BY rolname",
                (username, "enterprise_mind_%", "em_%"),
            )
            roles = [
                {
                    "name": row[0],
                    "superuser": bool(row[1]),
                    "createdb": bool(row[2]),
                    "createrole": bool(row[3]),
                    "replication": bool(row[4]),
                    "bypassrls": bool(row[5]),
                    "inherit": bool(row[6]),
                }
                for row in cursor.fetchall()
            ]
            cursor.execute(
                "SELECT grantee, table_schema, table_name, privilege_type "
                "FROM information_schema.role_table_grants "
                "WHERE table_schema = ANY(%s) "
                "AND (grantee = %s OR grantee LIKE %s OR grantee LIKE %s) "
                "ORDER BY grantee, table_schema, table_name, privilege_type",
                (list(APP_SCHEMAS), username, "enterprise_mind_%", "em_%"),
            )
            grants = [
                {
                    "grantee": row[0],
                    "schema": row[1],
                    "table": row[2],
                    "privilege": row[3],
                }
                for row in cursor.fetchall()
            ]
            role_metadata = {
                "roles_without_passwords": roles,
                "table_grants": grants,
                "source_scope": "application roles and grants only",
            }
    roles_metadata = output / "postgres" / "roles-and-grants.json"
    safe_json(roles_metadata, role_metadata)
    return {
        "dump": {
            "path": str(dump.relative_to(output)),
            "size": dump.stat().st_size,
            "sha256": sha256_file(dump),
            "format": "custom",
            "tool": "pg_dump",
        },
        "roles_and_grants": {
            "path": str(roles_metadata.relative_to(output)),
            "size": roles_metadata.stat().st_size,
            "sha256": sha256_file(roles_metadata),
            "passwords_exported": False,
            "cluster_wide_role_dump": False,
            "roles_without_passwords": roles,
            "table_grants": grants,
        },
        "host": host,
        "port": port,
        "identity": database_identity,
        "database": database,
        "username": username,
        "extensions": extensions,
        "migrations": migrations,
        "table_counts": counts,
        "sequences": sequences,
        "roles_without_passwords": roles,
    }


def minio_snapshot(
    *,
    endpoint: str,
    access_file: Path,
    secret_file: Path,
    bucket: str,
    output: Path,
) -> dict[str, Any]:
    client = Minio(endpoint, read_secret(access_file), read_secret(secret_file), secure=False)
    try:
        if not client.bucket_exists(bucket):
            raise BackupError("source MinIO bucket does not exist")
        object_root = output / "minio" / "objects"
        object_root.mkdir(parents=True)
        items: list[dict[str, Any]] = []
        for index, item in enumerate(client.list_objects(bucket, recursive=True)):
            stat = client.stat_object(bucket, item.object_name)
            target = object_root / f"{index:08d}.bin"
            response = client.get_object(bucket, item.object_name)
            digest = hashlib.sha256()
            size = 0
            try:
                with target.open("wb") as stream:
                    for block in response.stream(1024 * 1024):
                        size += len(block)
                        digest.update(block)
                        stream.write(block)
            finally:
                response.close()
                response.release_conn()
            if size != stat.size:
                raise BackupError("MinIO object size changed during snapshot")
            items.append(
                {
                    "key": item.object_name,
                    "relative_path": str(target.relative_to(output)),
                    "size": size,
                    "sha256": digest.hexdigest(),
                    "content_type": stat.content_type,
                    "etag": stat.etag,
                    "version_id": stat.version_id,
                }
            )
    except BackupError:
        raise
    except Exception as error:
        raise BackupError("MinIO snapshot failed") from error
    return {
        "endpoint": endpoint,
        "bucket": bucket,
        "objects": items,
        "object_count": len(items),
    }


def redis_snapshot(
    *, host: str, port: int, username: str, secret_file: Path, stream: str
) -> dict[str, Any]:
    try:
        client = redis.Redis(
            host=host,
            port=port,
            username=username,
            password=read_secret(secret_file),
            decode_responses=True,
            socket_timeout=5,
        )
        client.ping()
        exists = bool(client.exists(stream))
        groups = client.xinfo_groups(stream) if exists else []
        info = client.xinfo_stream(stream) if exists else {}
        return {
            "endpoint": f"{host}:{port}",
            "username": username,
            "stream": stream,
            "exists": exists,
            "length": int(info.get("length", 0)) if info else 0,
            "groups": [
                {
                    "name": group.get("name"),
                    "consumers": group.get("consumers"),
                    "pending": group.get("pending"),
                    "last_delivered_id": group.get("last-delivered-id"),
                }
                for group in groups
            ],
            "policy": {
                "queue": "rebuild_empty_and_expire_inflight",
                "cache": "rebuild_empty",
                "authentication": "not stored in Redis; rotate target token signing key",
            },
        }
    except Exception as error:
        raise BackupError("Redis snapshot failed") from error


def copy_release_materials(output: Path) -> list[dict[str, Any]]:
    candidates = (
        Path("backend/pyproject.toml"),
        Path("backend/uv.lock"),
        Path("frontend/package-lock.json"),
        Path("config/application.yaml"),
        Path("config/providers.yaml"),
        Path("config/rag.yaml"),
        Path("config/agent.yaml"),
    )
    material_root = output / "release-materials"
    entries: list[dict[str, Any]] = []
    for relative in candidates:
        source = REPO_ROOT / relative
        if not source.is_file():
            continue
        target = material_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        entries.append(
            {
                "path": str(relative),
                "sha256": sha256_file(source),
                "size": source.stat().st_size,
            }
        )
    migration_root = material_root / "backend" / "migrations"
    for source in sorted((REPO_ROOT / "backend" / "migrations").rglob("*.py")):
        target = migration_root / source.relative_to(REPO_ROOT / "backend" / "migrations")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        entries.append(
            {
                "path": str(source.relative_to(REPO_ROOT)),
                "sha256": sha256_file(source),
                "size": source.stat().st_size,
            }
        )
    return entries


def backup(args: argparse.Namespace) -> int:
    source_project = require_project(args.source_project, kind="source")
    run_id = require_run_id(args.run_id)
    marker = verify_source_marker(args.source_identity, source_project)
    if args.write_freeze_ack != "P10-10-SOURCE-WRITES-FROZEN":
        raise BackupError("a coordinated source write-freeze acknowledgement is required")
    marker_ports = marker.get("ports", {})
    if {
        "application_postgres": args.pg_port,
        "redis": args.redis_port,
        "minio": int(args.minio_endpoint.rsplit(":", 1)[-1]),
    } != {
        "application_postgres": marker_ports.get("application_postgres"),
        "redis": marker_ports.get("redis"),
        "minio": marker_ports.get("minio"),
    }:
        raise BackupError("source identity marker ports do not match requested resources")
    marker_db = marker.get("migration", {}).get("database")
    marker_user = marker.get("migration", {}).get("user")
    if marker_db and args.pg_database != marker_db:
        raise BackupError("source identity marker database does not match requested resource")
    if marker_user and args.pg_user != marker_user:
        raise BackupError("source identity marker database owner does not match requested resource")
    runtime_before = verify_source_runtime(args)
    ensure_empty_directory(args.output, label="backup output")
    started = datetime.now(UTC)
    try:
        pg = postgres_snapshot(
            host=args.pg_host,
            port=args.pg_port,
            database=args.pg_database,
            username=args.pg_user,
            secret_file=args.pg_password_file,
            output=args.output,
            pg_dump_bin=args.pg_dump_bin,
        )
        objects = minio_snapshot(
            endpoint=args.minio_endpoint,
            access_file=args.minio_access_file,
            secret_file=args.minio_secret_file,
            bucket=args.minio_bucket,
            output=args.output,
        )
        redis_state = redis_snapshot(
            host=args.redis_host,
            port=args.redis_port,
            username=args.redis_user,
            secret_file=args.redis_password_file,
            stream=args.redis_stream,
        )
        runtime_after = verify_source_runtime(args)
        if runtime_after != runtime_before:
            raise BackupError("source Docker identity changed during the backup window")
        materials = copy_release_materials(args.output)
        secret_fingerprints = {
            name: sha256_file(args.secret_dir / name)
            for name in SECRET_NAMES
            if (args.secret_dir / name).is_file()
        }
        manifest = {
            "kind": "enterprise-mind-p10-10-backup",
            "run_id": run_id,
            "created_at": started.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "source": {
                "project": source_project,
                "identity_marker": str(args.source_identity),
                "identity_marker_sha256": sha256_file(args.source_identity),
                "migration": marker.get("migration"),
                "runtime_before": runtime_before,
                "runtime_after": runtime_after,
                "write_freeze_ack": "P10-10-SOURCE-WRITES-FROZEN",
            },
            "postgres": pg,
            "minio": objects,
            "redis": redis_state,
            "release_materials": materials,
            "secrets": {
                "names": list(SECRET_NAMES),
                "fingerprints_only": secret_fingerprints,
                "plaintext_or_recoverable_secret_values_in_backup": False,
                "restore_requires_external_reprovisioning": True,
            },
            "restore_invariants": {
                "target_project_prefix": "em-p10-restore-",
                "target_data_volumes_must_be_new": True,
                "target_token_signing_key_must_rotate": True,
                "redis_queue_policy": "rebuild_empty_and_expire_inflight",
                "process_local_database_export_plans_restored": False,
                "checkpoint_rows_are_data_only_and_do_not_authorize_old_actions": True,
            },
        }
        safe_json(args.output / "backup-manifest.json", manifest)
        safe_json(
            args.output / "backup.marker.json",
            {
                "kind": manifest["kind"],
                "run_id": run_id,
                "source_project": source_project,
            },
        )
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "manifest": str(args.output / "backup-manifest.json"),
                    "object_count": objects["object_count"],
                }
            )
        )
        return 0
    except Exception as error:
        safe_json(
            args.output / "backup-failure.json",
            {"status": "FAILED", "run_id": run_id, "error_type": type(error).__name__},
        )
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__}))
        return 2


def target_empty_checks(args: argparse.Namespace, manifest: dict[str, Any]) -> dict[str, Any]:
    if args.target_project == manifest["source"]["project"]:
        raise BackupError("restore target project must differ from source project")
    require_project(args.target_project, kind="target")
    if not args.target_identity.is_file():
        raise BackupError("target identity marker is missing")
    target_identity = json.loads(args.target_identity.read_text(encoding="utf-8"))
    if (
        target_identity.get("project") != args.target_project
        or target_identity.get("identity_verified") is not True
    ):
        raise BackupError(
            "target identity marker is not verified for the requested restore project"
        )
    if target_identity.get("target_data_is_new") is not True:
        raise BackupError("target identity marker must attest to new, empty target data")
    if target_identity.get("resource_scope") != "EnterpriseMind-only":
        raise BackupError(
            "target identity marker must attest to the EnterpriseMind-only resource scope"
        )
    if args.target_pg_host not in {"127.0.0.1", "localhost", "::1"}:
        raise BackupError("target PostgreSQL host must be the verified local Docker endpoint")
    if args.target_redis_host not in {"127.0.0.1", "localhost", "::1"}:
        raise BackupError("target Redis host must be the verified local Docker endpoint")
    target_minio_host = args.target_minio_endpoint.rsplit(":", 1)[0]
    if target_minio_host not in {"127.0.0.1", "localhost", "::1"}:
        raise BackupError("target MinIO host must be the verified local Docker endpoint")
    source_ports = {
        int(manifest["postgres"].get("port", -1)),
        int(manifest["redis"].get("endpoint", ":-").rsplit(":", 1)[-1]),
        int(manifest["minio"].get("endpoint", ":-").rsplit(":", 1)[-1]),
    }
    target_ports = {args.target_pg_port, args.target_redis_port}
    target_minio_port = int(args.target_minio_endpoint.rsplit(":", 1)[-1])
    target_ports.add(target_minio_port)
    if source_ports & target_ports:
        raise BackupError("restore target port overlaps the verified source resource")
    if args.target_minio_endpoint == manifest["minio"].get("endpoint"):
        raise BackupError("restore MinIO endpoint must be an independent target")
    target_secret_dir = args.target_secret_dir
    required_target_secrets = (
        "postgres_owner_password",
        "minio_app_access",
        "minio_app_secret",
        "token_signing_key",
    )
    for name in required_target_secrets:
        path = target_secret_dir / name
        if not path.is_file():
            raise BackupError(f"target Secret is missing: {name}")
    redis_secret_file(target_secret_dir)
    old_key = manifest.get("secrets", {}).get("fingerprints_only", {}).get("token_signing_key")
    new_key = target_secret_dir / "token_signing_key"
    if not isinstance(old_key, str) or not re.fullmatch(r"[0-9a-f]{64}", old_key):
        raise BackupError("backup lacks the source token signing key fingerprint")
    if sha256_file(new_key) == old_key:
        raise BackupError("target token signing key must be newly provisioned")
    return {
        "target_project": args.target_project,
        "target_identity_sha256": sha256_file(args.target_identity),
        "target_identity_marker_attests_new_data": True,
        "target_data_is_new": None,
        "token_signing_key_rotated": True,
        "source_token_key_restored": False,
        "resource_scope": target_identity["resource_scope"],
        "target_ports": sorted(target_ports),
    }


def verify_docker_project(
    *,
    docker_bin: str,
    project: str,
    service_ports: dict[str, tuple[str, int, str | None]],
    label: str,
) -> dict[str, Any]:
    """Verify actual Docker labels, port bindings and dedicated volumes."""
    try:
        listed = subprocess.run(
            docker_command(
                docker_bin,
                "ps",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{.ID}}",
            ),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise BackupError(f"{label} Docker identity query is unavailable") from error
    if listed.returncode:
        raise BackupError(f"{label} Docker identity query failed")
    ids = [value.strip() for value in listed.stdout.splitlines() if value.strip()]
    if not ids:
        raise BackupError(f"{label} Docker Compose project has no running containers")
    try:
        inspected = subprocess.run(
            docker_command(docker_bin, "inspect", *ids),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise BackupError(f"{label} Docker inspect is unavailable") from error
    if inspected.returncode:
        raise BackupError(f"{label} Docker inspect failed")
    try:
        containers = json.loads(inspected.stdout)
    except json.JSONDecodeError as error:
        raise BackupError(f"{label} Docker inspect returned invalid identity data") from error
    by_service = {
        container.get("Config", {}).get("Labels", {}).get("com.docker.compose.service"): container
        for container in containers
    }
    result: dict[str, Any] = {"project": project, "services": {}}
    for service, (
        container_port,
        expected_port,
        volume_destination,
    ) in service_ports.items():
        container = by_service.get(service)
        if not container:
            raise BackupError(f"{label} Docker service is missing: {service}")
        labels = container.get("Config", {}).get("Labels", {})
        if labels.get("com.docker.compose.project") != project:
            raise BackupError(f"{label} Docker service has an unexpected project label: {service}")
        if not container.get("State", {}).get("Running"):
            raise BackupError(f"{label} Docker service is not running: {service}")
        bindings = container.get("NetworkSettings", {}).get("Ports", {}).get(container_port) or []
        host_ports = {int(binding["HostPort"]) for binding in bindings if binding.get("HostPort")}
        if expected_port not in host_ports:
            raise BackupError(f"{label} Docker service port does not match: {service}")
        host_ips = {binding.get("HostIp") for binding in bindings}
        if host_ips - {"127.0.0.1", "::1"}:
            raise BackupError(f"{label} Docker service is not loopback-bound: {service}")
        all_mounts = container.get("Mounts", [])
        if volume_destination is None:
            environment = {}
            for item in container.get("Config", {}).get("Env", []) or []:
                key, separator, value = item.partition("=")
                if separator:
                    environment[key] = value
            pgdata_value = environment.get("PGDATA")
            if not pgdata_value or not PurePosixPath(pgdata_value).is_absolute():
                raise BackupError(f"{label} PostgreSQL PGDATA is missing or invalid")
            pgdata = PurePosixPath(pgdata_value)
            matching_mounts = [
                mount
                for mount in all_mounts
                if PurePosixPath(str(mount.get("Destination", "/"))) == pgdata
                or PurePosixPath(str(mount.get("Destination", "/"))) in pgdata.parents
            ]
            if not matching_mounts:
                raise BackupError(f"{label} PostgreSQL data volume does not cover PGDATA")
            most_specific = max(
                len(PurePosixPath(str(mount["Destination"])).parts) for mount in matching_mounts
            )
            matching_mounts = [
                mount
                for mount in matching_mounts
                if len(PurePosixPath(str(mount["Destination"])).parts) == most_specific
            ]
            if any(
                mount.get("Type") != "volume" or not mount.get("Name") for mount in matching_mounts
            ):
                raise BackupError(f"{label} PostgreSQL PGDATA is shadowed by a non-volume mount")
            volume_destination = str(matching_mounts[0]["Destination"])
            volume_names = {mount.get("Name") for mount in matching_mounts}
        else:
            pgdata = None
            volume_names = {
                mount.get("Name")
                for mount in all_mounts
                if mount.get("Type") == "volume"
                and mount.get("Name")
                and mount.get("Destination") == volume_destination
            }
        dedicated_volumes: list[str] = []
        for name in volume_names:
            if not name:
                continue
            try:
                volume = subprocess.run(
                    docker_command(docker_bin, "volume", "inspect", name),
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError as error:
                raise BackupError(f"{label} Docker volume identity query is unavailable") from error
            if volume.returncode:
                raise BackupError(f"{label} Docker volume identity query failed: {service}")
            try:
                records = json.loads(volume.stdout)
            except json.JSONDecodeError as error:
                raise BackupError(
                    f"{label} Docker volume identity is invalid: {service}"
                ) from error
            if not records:
                raise BackupError(f"{label} Docker volume identity is empty: {service}")
            volume_labels = records[0].get("Labels") or {}
            if volume_labels.get("com.docker.compose.project") == project:
                dedicated_volumes.append(name)
        if not dedicated_volumes:
            raise BackupError(f"{label} Docker data volume is missing or reused: {service}")
        result["services"][service] = {
            "container_id": str(container.get("Id", ""))[:12],
            "image": container.get("Config", {}).get("Image"),
            "image_id": container.get("Image"),
            "host_ports": sorted(host_ports),
            "volume_names": sorted(dedicated_volumes),
            "volume_destinations": [volume_destination],
            **({"pgdata": str(pgdata)} if pgdata is not None else {}),
        }
    return result


def verify_target_runtime(args: argparse.Namespace) -> dict[str, Any]:
    """Verify the actual Docker resources before any target write."""

    return verify_docker_project(
        docker_bin=args.target_docker_bin,
        project=args.target_project,
        service_ports={
            args.target_postgres_service: (
                "5432/tcp",
                args.target_pg_port,
                None,
            ),
            args.target_redis_service: ("6379/tcp", args.target_redis_port, "/data"),
            args.target_minio_service: (
                "9000/tcp",
                int(args.target_minio_endpoint.rsplit(":", 1)[-1]),
                "/data",
            ),
        },
        label="target",
    )


def verify_source_runtime(args: argparse.Namespace) -> dict[str, Any]:
    """Verify current source containers, separate from the historical marker."""

    return verify_docker_project(
        docker_bin=args.source_docker_bin,
        project=args.source_project,
        service_ports={
            args.source_postgres_service: (
                "5432/tcp",
                args.pg_port,
                None,
            ),
            args.source_redis_service: ("6379/tcp", args.redis_port, "/data"),
            args.source_minio_service: (
                "9000/tcp",
                int(args.minio_endpoint.rsplit(":", 1)[-1]),
                "/data",
            ),
        },
        label="source",
    )


def restore(args: argparse.Namespace) -> int:
    writes_started = False
    try:
        if not args.dry_run and not args.allow_restore:
            raise BackupError("--allow-restore is required for target writes")
        manifest = json.loads((args.backup / "backup-manifest.json").read_text(encoding="utf-8"))
        if manifest.get("kind") != "enterprise-mind-p10-10-backup":
            raise BackupError("unsupported backup manifest kind")
        artifacts = validate_backup_artifacts(args.backup, manifest)
        target = target_empty_checks(args, manifest)
        run_checked(
            tool_command(
                args.pg_restore_bin, "PostgreSQL restore", "--list", str(artifacts["dump"])
            )
        )
        runtime = verify_target_runtime(args)
        target_postgres = verify_empty_postgres_target(args, manifest)
        target_minio = verify_empty_minio_target(args)
        target_redis = verify_empty_redis_target(args)
        target = {
            **target,
            "target_data_is_new": True,
            "token_signing_key_rotated": True,
            "prewrite_empty_checks": {
                "postgres_application_schemas": target_postgres["application_schema_count"],
                "postgres_roles": target_postgres["roles"],
                "minio": target_minio,
                "redis": target_redis,
            },
        }
        if args.dry_run:
            result = {
                "status": "PASS",
                "mode": "dry-run",
                "target": target,
                "runtime": runtime,
                "postgres_empty_verified": True,
                "minio_empty_verified": True,
                "redis_empty_verified": True,
                "writes_performed": False,
            }
            safe_json(args.evidence, result)
            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "mode": "dry-run",
                        "evidence": str(args.evidence),
                    }
                )
            )
            return 0

        owner_secret = args.target_secret_dir / "postgres_owner_password"

        def restore_with_env(env: dict[str, str]) -> None:
            run_checked(
                tool_command(
                    args.pg_restore_bin,
                    "PostgreSQL restore",
                    "--exit-on-error",
                    "--no-owner",
                    "--dbname",
                    f"postgresql://{args.target_pg_user}@{args.target_pg_host}:{args.target_pg_port}/{args.target_pg_database}",
                    str(artifacts["dump"]),
                ),
                env=env,
            )

        writes_started = True
        with_pgpass(owner_secret, args.target_pg_user, restore_with_env)
        restored_pg = verify_restored_postgres(args, manifest)
        restored_objects = restore_minio(args, manifest, artifacts["objects"])
        result = {
            "status": "PASS",
            "mode": "restore",
            "backup_manifest_sha256": sha256_file(args.backup / "backup-manifest.json"),
            "target": {
                **target,
            },
            "runtime": runtime,
            "postgres": restored_pg,
            "minio": restored_objects,
            "redis": target_redis,
            "recovery_policies": {
                "process_local_database_export_plans": "not_backed_up; must be regenerated",
                "checkpoint_rows": "data_only; current authorization and confirmation are required",
            },
            "snapshot_boundary": manifest.get("completed_at"),
        }
        safe_json(args.evidence, result)
        print(json.dumps({"status": "PASS", "evidence": str(args.evidence)}))
        return 0
    except Exception as error:
        status = "FAILED" if writes_started else "BLOCKED"
        safe_json(
            args.evidence,
            {
                "status": status,
                "error_type": type(error).__name__,
                "writes_started": writes_started,
            },
        )
        print(json.dumps({"status": status, "error_type": type(error).__name__}))
        return 2


def verify_restored_postgres(args: argparse.Namespace, manifest: dict[str, Any]) -> dict[str, Any]:
    connection = connect_pg(
        args.target_pg_host,
        args.target_pg_port,
        args.target_pg_database,
        args.target_pg_user,
        args.target_secret_dir / "postgres_owner_password",
    )
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM infra.alembic_version")
            migrations = [row[0] for row in cursor.fetchall()]
            expected_migrations = manifest["postgres"]["migrations"]
            if migrations != expected_migrations:
                raise BackupError("restored migration version differs from backup")
            counts: dict[str, int] = {}
            for table in CORE_TABLES:
                schema, name = table.split(".", 1)
                cursor.execute(f'SELECT count(*) FROM "{schema}"."{name}"')
                counts[table] = int(cursor.fetchone()[0])
            for table, expected in manifest["postgres"]["table_counts"].items():
                if counts.get(table) != expected:
                    raise BackupError(f"restored row count differs: {table}")
            role_check = verify_restored_roles(cursor, args, manifest)
            # The source may itself be a recovered database. Use a fresh name
            # and roll back the probe row so recovery never adds business data.
            # PostgreSQL sequences remain advanced across savepoint rollback.
            probe_name = "p10-restore-check-" + uuid4().hex
            cursor.execute("SAVEPOINT restore_identity_probe")
            try:
                cursor.execute(
                    "INSERT INTO iam.departments (name, name_key) VALUES (%s, %s) RETURNING id",
                    (probe_name, probe_name),
                )
                inserted_id = int(cursor.fetchone()[0])
            finally:
                cursor.execute("ROLLBACK TO SAVEPOINT restore_identity_probe")
                cursor.execute("RELEASE SAVEPOINT restore_identity_probe")
    return {
        "migrations": migrations,
        "table_counts": counts,
        "identity_insert_id": inserted_id,
        "identity_probe_row_persisted": False,
        "roles_and_grants": role_check,
    }


def verify_empty_postgres_target(
    args: argparse.Namespace, manifest: dict[str, Any]
) -> dict[str, Any]:
    connection = connect_pg(
        args.target_pg_host,
        args.target_pg_port,
        args.target_pg_database,
        args.target_pg_user,
        args.target_secret_dir / "postgres_owner_password",
    )
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_namespace WHERE nspname = ANY(%s)",
                (list(APP_SCHEMAS),),
            )
            schema_count = int(cursor.fetchone()[0])
            if schema_count != 0:
                raise BackupError("target PostgreSQL already has application schemas")
            roles = verify_preprovisioned_roles(cursor, args, manifest)
    return {"application_schema_count": schema_count, "roles": roles}


def verify_restored_roles(
    cursor: Any, args: argparse.Namespace, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Verify pre-created roles and grants restored from the PostgreSQL archive."""

    metadata = manifest.get("postgres", {}).get("roles_and_grants")
    if not isinstance(metadata, dict) or not metadata.get("path"):
        raise BackupError("backup lacks application role/grant metadata")
    role_check = verify_preprovisioned_roles(cursor, args, manifest)
    source_owner = manifest.get("postgres", {}).get("username")
    checked_grants = 0
    for grant in metadata.get("table_grants") or []:
        grantee = grant.get("grantee")
        schema = grant.get("schema")
        table = grant.get("table")
        privilege = grant.get("privilege")
        if (
            not isinstance(grantee, str)
            or not isinstance(schema, str)
            or not isinstance(table, str)
            or not isinstance(privilege, str)
            or schema not in APP_SCHEMAS
            or not re.fullmatch(r"[a-z_][a-z0-9_]*", table)
            or privilege
            not in {
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "TRUNCATE",
                "REFERENCES",
                "TRIGGER",
                "MAINTAIN",
            }
        ):
            raise BackupError("backup contains an invalid table grant entry")
        target_grantee = args.target_pg_user if grantee == source_owner else grantee
        cursor.execute(
            "SELECT has_table_privilege(%s, %s, %s)",
            (target_grantee, f"{schema}.{table}", privilege),
        )
        if cursor.fetchone()[0] is not True:
            raise BackupError(
                f"target table grant is missing: {target_grantee}/{schema}.{table}/{privilege}"
            )
        checked_grants += 1
    return {**role_check, "table_grants_checked": checked_grants}


def verify_preprovisioned_roles(
    cursor: Any, args: argparse.Namespace, manifest: dict[str, Any]
) -> dict[str, int]:
    """Check role presence and security flags before any target restore writes."""

    metadata = manifest.get("postgres", {}).get("roles_and_grants")
    if not isinstance(metadata, dict) or not metadata.get("path"):
        raise BackupError("backup lacks application role/grant metadata")
    roles = metadata.get("roles_without_passwords") or []
    if not isinstance(roles, list) or not roles:
        raise BackupError("backup lacks application role entries")
    source_owner = manifest.get("postgres", {}).get("username")
    checked_roles = 0
    for role in roles:
        if not isinstance(role, dict):
            raise BackupError("backup contains an invalid application role entry")
        source_name = role.get("name")
        if not isinstance(source_name, str) or not source_name:
            raise BackupError("backup contains an invalid application role entry")
        target_name = args.target_pg_user if source_name == source_owner else source_name
        cursor.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit "
            "FROM pg_roles WHERE rolname = %s",
            (target_name,),
        )
        actual = cursor.fetchone()
        if actual is None:
            raise BackupError(f"target application role is missing: {target_name}")
        expected_flags = tuple(
            bool(role.get(name))
            for name in (
                "superuser",
                "createdb",
                "createrole",
                "replication",
                "bypassrls",
                "inherit",
            )
        )
        if tuple(bool(value) for value in actual) != expected_flags:
            raise BackupError(f"target application role flags differ: {target_name}")
        checked_roles += 1
    return {"roles_checked": checked_roles}


def restore_minio(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    validated: list[tuple[dict[str, Any], Path]],
) -> dict[str, Any]:
    client = Minio(
        args.target_minio_endpoint,
        read_secret(args.target_secret_dir / "minio_app_access"),
        read_secret(args.target_secret_dir / "minio_app_secret"),
        secure=False,
    )
    if client.bucket_exists(args.target_minio_bucket):
        raise BackupError("target MinIO bucket already exists")
    client.make_bucket(args.target_minio_bucket)
    checked = 0
    for item, source in validated:
        with source.open("rb") as stream:
            client.put_object(
                args.target_minio_bucket,
                item["key"],
                stream,
                length=item["size"],
                content_type=item.get("content_type") or "application/octet-stream",
            )
        stat = client.stat_object(args.target_minio_bucket, item["key"])
        if stat.size != item["size"]:
            raise BackupError("restored MinIO object size mismatch")
        response = client.get_object(args.target_minio_bucket, item["key"])
        digest = hashlib.sha256()
        try:
            for block in response.stream(1024 * 1024):
                digest.update(block)
        finally:
            response.close()
            response.release_conn()
        if digest.hexdigest() != item["sha256"]:
            raise BackupError("restored MinIO object hash mismatch")
        checked += 1
    return {
        "bucket": args.target_minio_bucket,
        "object_count": checked,
        "hashes_verified": True,
    }


def verify_empty_redis_target(args: argparse.Namespace) -> dict[str, Any]:
    client = redis.Redis(
        host=args.target_redis_host,
        port=args.target_redis_port,
        username=args.target_redis_user,
        password=read_secret(redis_secret_file(args.target_secret_dir)),
        decode_responses=True,
        socket_timeout=5,
    )
    client.ping()
    size = int(client.dbsize())
    if size != 0:
        raise BackupError("target Redis is not empty; refusing to overwrite queue/cache state")
    return {
        "dbsize": size,
        "queue_policy": "rebuilt_empty",
        "authentication_policy": "token_key_rotated",
    }


def verify_empty_minio_target(args: argparse.Namespace) -> dict[str, Any]:
    try:
        client = Minio(
            args.target_minio_endpoint,
            read_secret(args.target_secret_dir / "minio_app_access"),
            read_secret(args.target_secret_dir / "minio_app_secret"),
            secure=False,
        )
        if client.bucket_exists(args.target_minio_bucket):
            raise BackupError("target MinIO bucket already exists")
    except BackupError:
        raise
    except Exception as error:
        raise BackupError("target MinIO emptiness check failed") from error
    return {"bucket": args.target_minio_bucket, "bucket_absent": True}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    backup_parser = commands.add_parser("backup")
    backup_parser.add_argument("--run-id", required=True)
    backup_parser.add_argument("--source-project", required=True)
    backup_parser.add_argument("--source-identity", type=Path, required=True)
    backup_parser.add_argument("--source-docker-bin", default="docker")
    backup_parser.add_argument("--source-postgres-service", default="postgresql")
    backup_parser.add_argument("--source-redis-service", default="redis")
    backup_parser.add_argument("--source-minio-service", default="minio")
    backup_parser.add_argument("--write-freeze-ack", required=True)
    backup_parser.add_argument("--output", type=Path, required=True)
    backup_parser.add_argument("--secret-dir", type=Path, required=True)
    backup_parser.add_argument("--pg-host", default="127.0.0.1")
    backup_parser.add_argument("--pg-dump-bin", default="pg_dump")
    backup_parser.add_argument("--pg-port", type=int, required=True)
    backup_parser.add_argument("--pg-database", required=True)
    backup_parser.add_argument("--pg-user", required=True)
    backup_parser.add_argument("--pg-password-file", type=Path, required=True)
    backup_parser.add_argument("--minio-endpoint", required=True)
    backup_parser.add_argument("--minio-access-file", type=Path, required=True)
    backup_parser.add_argument("--minio-secret-file", type=Path, required=True)
    backup_parser.add_argument("--minio-bucket", required=True)
    backup_parser.add_argument("--redis-host", default="127.0.0.1")
    backup_parser.add_argument("--redis-port", type=int, required=True)
    backup_parser.add_argument("--redis-user", default="enterprise_mind_app")
    backup_parser.add_argument("--redis-password-file", type=Path, required=True)
    backup_parser.add_argument("--redis-stream", required=True)
    backup_parser.set_defaults(function=backup)

    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("--backup", type=Path, required=True)
    restore_parser.add_argument("--target-project", required=True)
    restore_parser.add_argument("--target-identity", type=Path, required=True)
    restore_parser.add_argument("--target-secret-dir", type=Path, required=True)
    restore_parser.add_argument("--target-pg-host", default="127.0.0.1")
    restore_parser.add_argument("--target-pg-port", type=int, required=True)
    restore_parser.add_argument("--target-pg-database", required=True)
    restore_parser.add_argument("--target-pg-user", required=True)
    restore_parser.add_argument("--pg-restore-bin", default="pg_restore")
    restore_parser.add_argument("--target-minio-endpoint", required=True)
    restore_parser.add_argument("--target-minio-bucket", required=True)
    restore_parser.add_argument("--target-redis-host", default="127.0.0.1")
    restore_parser.add_argument("--target-redis-port", type=int, required=True)
    restore_parser.add_argument("--target-redis-user", default="enterprise_mind_app")
    restore_parser.add_argument("--target-docker-bin", default="docker")
    restore_parser.add_argument("--target-postgres-service", default="postgres")
    restore_parser.add_argument("--target-redis-service", default="redis")
    restore_parser.add_argument("--target-minio-service", default="minio")
    restore_parser.add_argument("--evidence", type=Path, required=True)
    restore_parser.add_argument("--dry-run", action="store_true")
    restore_parser.add_argument("--allow-restore", action="store_true")
    restore_parser.set_defaults(function=restore)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "restore" and not args.dry_run and not args.allow_restore:
        safe_json(args.evidence, {"status": "BLOCKED", "reason": "allow_restore_required"})
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": "--allow-restore is required for target writes",
                }
            )
        )
        return 2
    try:
        return int(args.function(args))
    except BackupError as error:
        print(json.dumps({"status": "BLOCKED", "error_type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
