"""Verify one real Phase-10 database-export process chain.

This is an acceptance probe, not a fixture or an in-process test.  It creates
one export through a running API, observes the durable outbox task and Redis
transport, optionally consumes the opaque plan directly through the independent
MCP owner, waits for an independently running Worker, and verifies the final
PostgreSQL/MinIO/file facts.  The API, Dispatcher, Worker and MCP PIDs and all
resource endpoints must be supplied in a resource manifest that was produced
by the Phase-10 operator after checking the dedicated Compose project.

The manifest shape is intentionally small and non-secret::

    {"project": "em-p10-20260921-lm01", "identity_verified": true,
     "ports": {"application_postgres": 25432, "business_postgres": 25433,
               "redis": 26379, "minio": 29000},
     "redis_stream": "enterprise-mind:phase10:<run-id>",
     "minio_bucket": "<run-bucket>",
     "endpoints": {"api": "http://127.0.0.1:28000",
                    "mcp": "http://127.0.0.1:28001/mcp"},
     "pids": {"api": 1, "dispatcher": 2, "worker": 3, "mcp": 4}}

The script deliberately fails closed.  It never starts, stops, deletes,
truncates or recreates a service.  Passwords and bearer/MCP tokens are read
from files and are never written to evidence.  SQL, bound parameters, result
rows and model data are not requested from the API or written to evidence;
the SQL below only reads fixed task/file/queue metadata from the application
database and the fixed Phase-10 fixture count from the business database.

Example (all secret paths are controlled local files)::

    python scripts/phase10_export_process_chain.py \
      --resource-manifest .local/phase10/.../process-resources.json \
      --token-file .local/phase10/.../user-token \
      --mcp-token-file .local/test-secrets/phase10_mcp_token \
      --app-password-file .local/test-secrets/postgres_password \
      --business-password-file .local/test-secrets/phase10_business_readonly_password \
      --minio-access-file .local/test-secrets/minio_app_access \
      --minio-secret-file .local/test-secrets/minio_app_secret \
      --conversation-id 123 --query-id query-id-from-the-real-session \
      --result-id page-id-from-the-real-session --scope current_page \
      --evidence-dir .local/phase10/.../export-process-chain

The user token and query/result IDs must come from an authorized, real API
session.  Synthetic IDs, an ASGITransport app, or a fixture-only Worker do not
constitute a pass.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

EXPECTED_PROJECT = "em-p10-20260921-lm01"
EXPECTED_APP_PORT = 25432
EXPECTED_BUSINESS_PORT = 25433
EXPECTED_REDIS_PORT = 26379
EXPECTED_MINIO_PORT = 29000
EXPECTED_APP_DATABASE = "em_stage1_test"
EXPECTED_BUSINESS_DATABASE = "em_business_test"
EXPECTED_BUSINESS_USER = "em_p10_business_readonly"
EXPECTED_BUSINESS_ROWS = 235
MCP_PROTOCOL_VERSION = "2026-07-28"
REDIS_GROUP = "ingestion-workers"
DEFAULT_REDIS_STREAM = "enterprise-mind:processing"
ALLOWED_INPUT_REF_KEYS = {"runtime_handle", "file_name", "row_limit"}
FORBIDDEN_DURABLE_KEYS = {
    "sql",
    "query_sql",
    "validated_query",
    "validated_sql",
    "parameters",
    "params",
    "bound_values",
    "rows",
    "result_rows",
    "prompt",
    "response",
}
SAFE_STATUS_FIELDS = {
    "task_id",
    "kind",
    "status",
    "phase",
    "format",
    "scope",
    "processed_rows",
    "total_rows",
    "exported_rows",
    "file_id",
    "complete",
    "error",
}


class VerificationBlocked(RuntimeError):
    """The required real dependency or authorized input is not available."""


class VerificationFailed(RuntimeError):
    """A real dependency responded, but an acceptance assertion failed."""


def _read_secret(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VerificationBlocked(f"cannot read controlled secret file: {path}") from exc
    if not value:
        raise VerificationBlocked(f"controlled secret file is empty: {path}")
    return value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _contains_forbidden_key(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_DURABLE_KEYS:
                return str(key)
            found = _contains_forbidden_key(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _contains_forbidden_key(item)
            if found:
                return found
    return None


def _mask_handle(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return f"sha256:{_sha256_text(value)[:16]}"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows accepts os.kill(pid, 0) inconsistently.  On this host it
        # returns WinError 87 for a valid Python listener, so query the
        # process with limited information access instead.  This proves
        # liveness without granting control or changing process state.
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        # A protected process is still present; its identity is supplied by the
        # operator manifest and the liveness check remains conservative.
        return True
    except OSError:
        return False
    return True


def _parse_pid(value: Any, role: str) -> int:
    try:
        pid = int(value)
    except (TypeError, ValueError) as exc:
        raise VerificationBlocked(f"resource manifest has no valid {role} PID") from exc
    if pid <= 0:
        raise VerificationBlocked(f"resource manifest has no valid {role} PID")
    return pid


def _url_host_port(url: str) -> tuple[str, int | None]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise VerificationBlocked(f"invalid endpoint URL: {url}")
    return parsed.hostname, parsed.port


def _validate_manifest(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationBlocked(f"cannot read resource manifest: {path}") from exc
    if not isinstance(manifest, dict):
        raise VerificationBlocked("resource manifest must be a JSON object")
    if manifest.get("project") != EXPECTED_PROJECT:
        raise VerificationBlocked("resource manifest is for a different Compose project")
    if manifest.get("identity_verified") is not True:
        raise VerificationBlocked("resource identity was not marked verified by the operator")
    ports = manifest.get("ports")
    if not isinstance(ports, dict):
        raise VerificationBlocked("resource manifest must include the isolated host ports")
    expected_ports = {
        "application_postgres": EXPECTED_APP_PORT,
        "business_postgres": EXPECTED_BUSINESS_PORT,
        "redis": EXPECTED_REDIS_PORT,
        "minio": EXPECTED_MINIO_PORT,
    }
    for name, expected in expected_ports.items():
        if ports.get(name) != expected:
            raise VerificationBlocked(f"resource manifest has an unexpected {name} port")

    pids = manifest.get("pids")
    if not isinstance(pids, dict):
        raise VerificationBlocked("resource manifest must include API/Dispatcher/Worker/MCP PIDs")
    roles = ("api", "dispatcher", "worker", "mcp")
    for role in roles:
        pid = _parse_pid(pids.get(role), role)
        if not _pid_alive(pid):
            raise VerificationBlocked(f"{role} process PID {pid} is not alive")

    endpoints = manifest.get("endpoints")
    if not isinstance(endpoints, dict):
        raise VerificationBlocked("resource manifest must include API and MCP endpoints")
    if endpoints.get("api") != args.api_url:
        raise VerificationBlocked("API URL does not match the verified resource manifest")
    if endpoints.get("mcp") != args.mcp_url:
        raise VerificationBlocked("MCP URL does not match the verified resource manifest")
    manifest_stream = manifest.get("redis_stream")
    if not isinstance(manifest_stream, str) or not manifest_stream.startswith("enterprise-mind:"):
        raise VerificationBlocked("resource manifest must include the isolated Redis stream")
    if args.redis_stream != manifest_stream:
        raise VerificationBlocked("Redis stream does not match the verified resource manifest")
    manifest_bucket = manifest.get("minio_bucket")
    if not isinstance(manifest_bucket, str) or not manifest_bucket:
        raise VerificationBlocked("resource manifest must include the isolated MinIO bucket")
    if args.minio_bucket != manifest_bucket:
        raise VerificationBlocked("MinIO bucket does not match the verified resource manifest")
    return manifest


def _pid_snapshot(manifest: dict[str, Any]) -> dict[str, bool]:
    pids = manifest["pids"]
    return {role: _pid_alive(_parse_pid(pids.get(role), role)) for role in ("api", "dispatcher", "worker", "mcp")}


def _parse_api_task(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise VerificationFailed("API response is not a JSON object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise VerificationFailed("API response has no data envelope")
    return data


def _safe_api_status(data: dict[str, Any]) -> dict[str, Any]:
    result = {key: _safe_json_value(data.get(key)) for key in SAFE_STATUS_FIELDS if key in data}
    error = result.get("error")
    if isinstance(error, dict):
        result["error"] = {key: error.get(key) for key in ("code", "message") if key in error}
    return result


def _require_http_ok(response: Any, description: str) -> None:
    if response.status_code >= 500:
        raise VerificationBlocked(f"{description} returned HTTP {response.status_code}")
    if response.status_code >= 400:
        raise VerificationFailed(f"{description} returned HTTP {response.status_code}")


def _redis_snapshot(redis_client: Any, stream: str, group: str) -> dict[str, Any]:
    try:
        info = redis_client.xinfo_stream(stream)
    except Exception as exc:  # noqa: BLE001 - this is a read-only evidence probe
        return {"exists": False, "error_type": type(exc).__name__}
    result: dict[str, Any] = {
        "exists": True,
        "length": int(info.get("length", 0)),
        "last_generated_id": str(info.get("last-generated-id", "")),
    }
    try:
        groups = redis_client.xinfo_groups(stream)
    except Exception as exc:  # noqa: BLE001 - this is a read-only evidence probe
        result["group_error_type"] = type(exc).__name__
        return result
    group_info = next((item for item in groups if str(item.get("name")) == group), None)
    if group_info is None:
        result["group"] = None
        return result
    result["group"] = {
        "name": group,
        "consumers": int(group_info.get("consumers", 0)),
        "pending": int(group_info.get("pending", 0)),
        "last_delivered_id": str(group_info.get("last-delivered-id", "")),
    }
    return result


def _redis_find_event(redis_client: Any, stream: str, event_id: int) -> str | None:
    try:
        entries = redis_client.xrevrange(stream, count=100)
    except Exception:  # noqa: BLE001 - Redis availability is a bounded probe
        return None
    for message_id, fields in entries:
        if str(fields.get("event_id")) == str(event_id):
            return str(message_id)
    return None


def _connect_app(args: argparse.Namespace) -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise VerificationBlocked("psycopg is required for application fact verification") from exc
    password = _read_secret(args.app_password_file)
    try:
        return psycopg.connect(
            host=args.app_host,
            port=args.app_port,
            dbname=args.app_database,
            user=args.app_user,
            password=password,
            connect_timeout=5,
            application_name="enterprise_mind_phase10_export_acceptance",
        )
    except Exception as exc:
        raise VerificationBlocked("isolated application PostgreSQL is not ready") from exc


def _business_fact(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import psycopg
    except ImportError as exc:
        raise VerificationBlocked("psycopg is required for business-source verification") from exc
    password = _read_secret(args.business_password_file)
    try:
        with psycopg.connect(
            host=args.business_host,
            port=args.business_port,
            dbname=args.business_database,
            user=args.business_user,
            password=password,
            connect_timeout=5,
            application_name="enterprise_mind_phase10_export_acceptance_source",
        ) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_database(), current_user, count(*) "
                "FROM business.phase10_orders"
            )
            database, user, count = cursor.fetchone()
    except Exception as exc:
        raise VerificationBlocked("isolated business PostgreSQL fixture is not ready") from exc
    if database != EXPECTED_BUSINESS_DATABASE or user != EXPECTED_BUSINESS_USER:
        raise VerificationFailed("business source identity does not match the read-only fixture role")
    if int(count) != EXPECTED_BUSINESS_ROWS:
        raise VerificationFailed(f"business fixture row count is {count}, expected {EXPECTED_BUSINESS_ROWS}")
    return {
        "host": args.business_host,
        "port": args.business_port,
        "database": database,
        "user": user,
        "row_count": int(count),
        # phase10_fixture_meta is intentionally hidden from the read-only
        # source role.  Seed/hash provenance is captured in the operator's
        # fixture evidence; this live probe verifies the source identity and
        # the actual 235-row count without widening that role's grants.
        "fixture_metadata": "operator-fixture-evidence",
    }


def _task_fact(connection: Any, task_id: int) -> dict[str, Any]:
    query = """
        SELECT
            t.id, t.owner_user_id, t.conversation_id, t.kind, t.format, t.scope,
            t.export_plan_id, t.input_ref, t.source_refs, t.status, t.phase,
            t.processed_rows, t.total_rows, t.exported_rows, t.file_id,
            t.error_code, t.error_message,
            p.id, p.runtime_handle, p.query_id, p.result_id, p.scope, p.database_source_id,
            p.schema_snapshot_id, p.source_version, p.expires_at,
            e.id, e.event_type, e.status, e.payload, e.attempt_count,
            gf.id, gf.storage_object_id, gf.file_name, gf.file_format,
            gf.generation_kind, gf.result_scope, gf.exported_rows,
            gf.content_sha256, gf.size_bytes,
            so.bucket_name, so.object_key, so.object_type, so.status,
            so.size_bytes, so.content_sha256
        FROM agent.generation_tasks AS t
        LEFT JOIN agent.database_export_plans AS p ON p.id = t.export_plan_id
        LEFT JOIN infra.outbox_events AS e
          ON e.aggregate_type = 'generation_task'
         AND e.aggregate_id = t.id
         AND e.event_type = 'generation.export.requested'
        LEFT JOIN agent.generated_files AS gf ON gf.task_id = t.id
        LEFT JOIN infra.storage_objects AS so ON so.id = gf.storage_object_id
        WHERE t.id = %s
        ORDER BY e.id DESC NULLS LAST
        LIMIT 1
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, (task_id,))
            row = cursor.fetchone()
    except Exception as exc:
        raise VerificationBlocked("cannot read application task/file facts") from exc
    if row is None:
        raise VerificationFailed(f"application task {task_id} does not exist")
    names = (
        "id", "owner_user_id", "conversation_id", "kind", "format", "scope", "export_plan_id",
        "input_ref", "source_refs", "status", "phase", "processed_rows", "total_rows",
        "exported_rows", "file_id", "error_code", "error_message", "plan_id", "plan_handle",
        "plan_query_id", "plan_result_id", "plan_scope", "database_source_id", "schema_snapshot_id",
        "source_version", "expires_at", "event_id", "event_type", "event_status", "event_payload",
        "event_attempt_count", "generated_file_id", "storage_object_id", "file_name", "file_format",
        "generation_kind", "result_scope", "file_exported_rows", "file_sha256", "file_size_bytes",
        "bucket_name", "object_key", "object_type", "object_status", "object_size_bytes",
        "object_sha256",
    )
    return dict(zip(names, row, strict=True))


def _safe_fact(fact: dict[str, Any]) -> dict[str, Any]:
    input_ref = _json_object(fact.get("input_ref"))
    source_refs = fact.get("source_refs")
    payload = _json_object(fact.get("event_payload"))
    safe_input = {
        "keys": sorted(str(key) for key in input_ref),
        "runtime_handle": _mask_handle(input_ref.get("runtime_handle")),
        "file_name_present": input_ref.get("file_name") is not None,
        "row_limit": input_ref.get("row_limit"),
    }
    source_keys = sorted(str(key) for item in source_refs or [] if isinstance(item, dict) for key in item)
    return {
        "task": {
            key: _safe_json_value(fact.get(key))
            for key in (
                "id", "owner_user_id", "conversation_id", "kind", "format", "scope",
                "export_plan_id", "status", "phase", "processed_rows", "total_rows",
                "exported_rows", "file_id", "error_code",
            )
        },
        "input_ref": safe_input,
        "source_ref_keys": source_keys,
        "plan": {
            "present": fact.get("plan_id") is not None,
            "id": fact.get("plan_id"),
            "runtime_handle": _mask_handle(fact.get("plan_handle")),
            "query_id_present": fact.get("plan_query_id") is not None,
            "result_id_present": fact.get("plan_result_id") is not None,
            "scope": fact.get("plan_scope"),
            "database_source_id": fact.get("database_source_id"),
            "schema_snapshot_id": fact.get("schema_snapshot_id"),
            "source_version": fact.get("source_version"),
            "expires_at": fact.get("expires_at").isoformat() if fact.get("expires_at") else None,
        },
        "outbox": {
            "id": fact.get("event_id"),
            "event_type": fact.get("event_type"),
            "status": fact.get("event_status"),
            "payload_keys": sorted(payload),
            "payload": _safe_json_value(payload),
            "attempt_count": fact.get("event_attempt_count"),
        },
        "file": {
            "id": fact.get("generated_file_id"),
            "storage_object_id": fact.get("storage_object_id"),
            "file_name": fact.get("file_name"),
            "file_format": fact.get("file_format"),
            "generation_kind": fact.get("generation_kind"),
            "result_scope": fact.get("result_scope"),
            "exported_rows": fact.get("file_exported_rows"),
            "sha256": fact.get("file_sha256"),
            "size_bytes": fact.get("file_size_bytes"),
        },
        "storage_object": {
            "bucket": fact.get("bucket_name"),
            "object_key_present": fact.get("object_key") is not None,
            "object_type": fact.get("object_type"),
            "status": fact.get("object_status"),
            "size_bytes": fact.get("object_size_bytes"),
            "sha256": fact.get("object_sha256"),
        },
    }


def _assert_durable_boundary(fact: dict[str, Any]) -> None:
    input_ref = _json_object(fact.get("input_ref"))
    if set(input_ref) != ALLOWED_INPUT_REF_KEYS:
        raise VerificationFailed("durable task input_ref contains fields outside the opaque export contract")
    for label, value in (
        ("input_ref", input_ref),
        ("source_refs", fact.get("source_refs")),
        ("outbox payload", fact.get("event_payload")),
    ):
        forbidden = _contains_forbidden_key(value)
        if forbidden:
            raise VerificationFailed(f"{label} persisted forbidden export field {forbidden}")
    payload = _json_object(fact.get("event_payload"))
    if set(payload) != {"task_id"} or str(payload.get("task_id")) != str(fact["id"]):
        raise VerificationFailed("generation export outbox payload is not the opaque task reference")


def _owner_stream_probe(args: argparse.Namespace, handle: str, owner_user_id: int, conversation_id: int) -> dict[str, Any]:
    try:
        import httpx
    except ImportError as exc:
        raise VerificationBlocked("httpx is required for the independent MCP probe") from exc
    token = _read_secret(args.mcp_token_file)
    endpoint = args.mcp_url.rstrip("/")
    endpoint = endpoint.removesuffix("/mcp")
    endpoint += "/export-plan/stream"
    headers = {
        "X-EnterpriseMind-MCP-Secret": token,
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/x-ndjson",
    }
    body = {
        "handle": handle,
        "owner_user_id": owner_user_id,
        "conversation_id": conversation_id,
        "max_rows": args.owner_probe_max_rows,
        "batch_size": min(args.owner_probe_max_rows, 1000),
        "max_serialized_bytes": 100 * 1024 * 1024,
        "timeout_seconds": min(args.timeout_seconds, 120),
    }
    saw_header = False
    saw_end = False
    frame_types: list[str] = []
    rows = 0
    try:
        with httpx.Client(timeout=httpx.Timeout(args.timeout_seconds + 5, connect=5), trust_env=False) as client, client.stream(
            "POST", endpoint, headers=headers, json=body
        ) as response:
                if response.status_code != 200:
                    raise VerificationFailed(f"independent MCP owner stream returned HTTP {response.status_code}")
                for line in response.iter_lines():
                    if not line:
                        continue
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise VerificationFailed("independent MCP owner returned invalid NDJSON") from exc
                    if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
                        raise VerificationFailed("independent MCP owner returned an invalid frame")
                    frame_type = frame["type"]
                    frame_types.append(frame_type)
                    if frame_type == "error":
                        raise VerificationFailed(f"independent MCP owner returned {frame.get('error_code')}")
                    if frame_type == "header":
                        if saw_header or saw_end:
                            raise VerificationFailed("independent MCP owner returned duplicate header")
                        saw_header = True
                    elif frame_type == "batch":
                        if not saw_header or saw_end or not isinstance(frame.get("rows"), list):
                            raise VerificationFailed("independent MCP owner returned an invalid batch")
                        rows += len(frame["rows"])
                    elif frame_type == "end":
                        if not saw_header or saw_end or frame.get("complete") is not True:
                            raise VerificationFailed("independent MCP owner returned an invalid end frame")
                        if frame.get("exported_rows") != rows:
                            raise VerificationFailed("independent MCP owner row count is inconsistent")
                        saw_end = True
                    else:
                        raise VerificationFailed("independent MCP owner returned an unknown frame")
    except httpx.TimeoutException as exc:
        raise VerificationBlocked("independent MCP owner stream timed out") from exc
    if not saw_header or not saw_end:
        raise VerificationFailed("independent MCP owner stream ended without header/end")
    return {"ok": True, "frame_types": frame_types, "exported_rows": rows}


def _download_and_verify_file(
    args: argparse.Namespace,
    *,
    file_id: int,
    expected_sha256: str,
    expected_size: int,
    output_path: Path,
) -> dict[str, Any]:
    try:
        import httpx
    except ImportError as exc:
        raise VerificationBlocked("httpx is required for generated-file verification") from exc
    token = _read_secret(args.token_file)
    url = f"{args.api_url.rstrip('/')}/api/v1/generated-files/{file_id}/download"
    digest = hashlib.sha256()
    size = 0
    try:
        with httpx.stream("GET", url, headers={"Authorization": f"Bearer {token}"}, timeout=30, follow_redirects=False, trust_env=False) as response:
            if response.status_code >= 500:
                raise VerificationBlocked(f"generated-file download returned HTTP {response.status_code}")
            if response.status_code != 200:
                raise VerificationFailed(f"generated-file download returned HTTP {response.status_code}")
            with output_path.open("wb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > args.max_download_bytes:
                        raise VerificationFailed("download exceeded the acceptance file-size guard")
                    digest.update(chunk)
                    output.write(chunk)
    except OSError as exc:
        raise VerificationFailed("cannot write generated-file evidence") from exc
    actual = digest.hexdigest()
    if size != int(expected_size) or actual != expected_sha256:
        raise VerificationFailed("downloaded file hash/size does not match GeneratedFile")
    return {"path": str(output_path), "size_bytes": size, "sha256": actual}


def _minio_stat(args: argparse.Namespace, *, bucket: str, object_key: str) -> dict[str, Any]:
    try:
        from minio import Minio
    except ImportError as exc:
        raise VerificationBlocked("minio is required for direct object verification") from exc
    access = _read_secret(args.minio_access_file)
    secret = _read_secret(args.minio_secret_file)
    endpoint = args.minio_endpoint
    try:
        client = Minio(endpoint, access_key=access, secret_key=secret, secure=False)
        observed = client.stat_object(bucket, object_key)
    except Exception as exc:
        raise VerificationBlocked("MinIO object is not readable with the application identity") from exc
    if int(observed.size) < 0:
        raise VerificationFailed("MinIO returned an invalid object size")
    return {"bucket": bucket, "object_key_present": True, "size_bytes": int(observed.size), "etag": observed.etag}


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-manifest", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:28000")
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--mcp-token-file", type=Path, required=True)
    parser.add_argument("--app-host", default="127.0.0.1")
    parser.add_argument("--app-port", type=int, default=EXPECTED_APP_PORT)
    parser.add_argument("--app-database", default=EXPECTED_APP_DATABASE)
    parser.add_argument("--app-user", default="em_test_owner")
    parser.add_argument("--app-password-file", type=Path, required=True)
    parser.add_argument("--business-host", default="127.0.0.1")
    parser.add_argument("--business-port", type=int, default=EXPECTED_BUSINESS_PORT)
    parser.add_argument("--business-database", default=EXPECTED_BUSINESS_DATABASE)
    parser.add_argument("--business-user", default=EXPECTED_BUSINESS_USER)
    parser.add_argument("--business-password-file", type=Path, required=True)
    parser.add_argument("--redis-host", default="127.0.0.1")
    parser.add_argument("--redis-port", type=int, default=EXPECTED_REDIS_PORT)
    parser.add_argument("--redis-user", default="enterprise_mind_app")
    parser.add_argument("--redis-password-file", type=Path, required=True)
    parser.add_argument("--redis-stream", default=DEFAULT_REDIS_STREAM)
    parser.add_argument("--redis-group", default=REDIS_GROUP)
    parser.add_argument("--minio-endpoint", default="127.0.0.1:29000")
    parser.add_argument("--minio-bucket", default="enterprise-mind")
    parser.add_argument("--minio-access-file", type=Path, required=True)
    parser.add_argument("--minio-secret-file", type=Path, required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--query-id", required=True)
    parser.add_argument("--result-id")
    parser.add_argument("--scope", choices=("current_page", "all_matching", "first_n"), required=True)
    parser.add_argument("--row-limit", type=int)
    parser.add_argument("--file-name", default="phase10-real-export.xlsx")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--owner-probe-max-rows", type=int, default=1000)
    parser.add_argument("--no-owner-probe", action="store_true")
    parser.add_argument("--max-download-bytes", type=int, default=150 * 1024 * 1024)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.scope == "current_page" and not args.result_id:
        raise VerificationBlocked("current_page requires the real result_id from the authorized session")
    if args.scope != "current_page" and args.result_id:
        raise VerificationBlocked("result_id is only valid for current_page")
    if args.scope == "first_n" and not (isinstance(args.row_limit, int) and 1 <= args.row_limit <= 10_000):
        raise VerificationBlocked("first_n requires row_limit 1..10000")
    if args.scope != "first_n" and args.row_limit is not None:
        raise VerificationBlocked("row_limit is only valid for first_n")
    if not (1 <= args.timeout_seconds <= 600):
        raise VerificationBlocked("timeout-seconds must be 1..600")
    if not (1 <= args.owner_probe_max_rows <= 10_000):
        raise VerificationBlocked("owner-probe-max-rows must be 1..10000")
    if not args.redis_stream.startswith("enterprise-mind:") or args.redis_group != REDIS_GROUP:
        raise VerificationBlocked("refusing an unexpected Redis stream/group namespace")
    if args.app_port != EXPECTED_APP_PORT or args.business_port != EXPECTED_BUSINESS_PORT:
        raise VerificationBlocked("refusing non-Phase-10 PostgreSQL ports")
    if args.redis_port != EXPECTED_REDIS_PORT:
        raise VerificationBlocked("refusing non-Phase-10 Redis port")
    minio_host, minio_port = _url_host_port(f"http://{args.minio_endpoint}")
    if minio_host != "127.0.0.1" or minio_port != EXPECTED_MINIO_PORT:
        raise VerificationBlocked("refusing non-Phase-10 MinIO endpoint")
    try:
        int(args.conversation_id)
    except ValueError as exc:
        raise VerificationBlocked("conversation-id must be the real numeric conversation id") from exc


def main(argv: list[str] | None = None) -> int:
    parser = _make_parser()
    args = parser.parse_args(argv)
    evidence_dir = args.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / "result.json"
    result: dict[str, Any] = {
        "status": "not_started",
        "generated_at": datetime.now(UTC).isoformat(),
        "command": [arg for arg in (argv if argv is not None else sys.argv[1:])],
        "candidate": {"script_sha256": _sha256_text(Path(__file__).read_text(encoding="utf-8"))},
        "evidence_dir": str(evidence_dir),
    }

    def write_result() -> None:
        evidence_path.write_text(
            json.dumps(_safe_json_value(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    try:
        _validate_args(args)
        manifest = _validate_manifest(args.resource_manifest, args)
        result["resource_manifest"] = str(args.resource_manifest.resolve())
        result["project"] = manifest.get("project")
        result["pids_before"] = _pid_snapshot(manifest)

        # Import dependencies only after all fail-closed identity checks.
        try:
            import httpx
            import redis
        except ImportError as exc:
            raise VerificationBlocked("httpx and redis are required for the real process probe") from exc

        result["business_source"] = _business_fact(args)
        app_connection = _connect_app(args)
        try:
            redis_password = _read_secret(args.redis_password_file)
            redis_client = redis.Redis(
                host=args.redis_host,
                port=args.redis_port,
                username=args.redis_user,
                password=redis_password,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            try:
                redis_client.ping()
            except Exception as exc:
                raise VerificationBlocked("isolated Redis is not ready for the application identity") from exc
            result["redis_before"] = _redis_snapshot(redis_client, args.redis_stream, args.redis_group)

            api_token = _read_secret(args.token_file)
            idempotency_key = args.idempotency_key or f"phase10-real-export-{int(time.time())}"
            payload: dict[str, Any] = {
                "scope": args.scope,
                "query_id": args.query_id,
                "file_name": args.file_name,
            }
            if args.result_id is not None:
                payload["result_id"] = args.result_id
            if args.row_limit is not None:
                payload["row_limit"] = args.row_limit

            base_url = args.api_url.rstrip("/")
            headers = {
                "Authorization": f"Bearer {api_token}",
                "Idempotency-Key": idempotency_key,
                "Accept": "application/json",
            }
            with httpx.Client(base_url=base_url, headers=headers, timeout=15, follow_redirects=False, trust_env=False) as client:
                live = client.get("/health/live")
                _require_http_ok(live, "API /health/live")
                result["api_live_status"] = live.status_code
                ready = client.get("/health/ready")
                _require_http_ok(ready, "API /health/ready")
                result["api_ready_status"] = ready.status_code
                try:
                    ready_payload = ready.json()
                    if isinstance(ready_payload, dict):
                        result["api_ready_keys"] = sorted(ready_payload)
                except ValueError:
                    result["api_ready_keys"] = []

                response = client.post(
                    f"/api/v1/conversations/{int(args.conversation_id)}/database-exports",
                    json=payload,
                )
                if response.status_code >= 500:
                    raise VerificationBlocked(f"API database-export create returned HTTP {response.status_code}")
                if response.status_code != 202:
                    raise VerificationFailed(f"API database-export create returned HTTP {response.status_code}")
                task_data = _parse_api_task(response.json())
                task_id_text = task_data.get("task_id")
                try:
                    task_id = int(task_id_text)
                except (TypeError, ValueError) as exc:
                    raise VerificationFailed("API create response did not contain a numeric task_id") from exc
                result["create"] = _safe_api_status(task_data)

                initial_fact = _task_fact(app_connection, task_id)
                _assert_durable_boundary(initial_fact)
                result["registration_fact"] = _safe_fact(initial_fact)
                if initial_fact.get("kind") != "database_export" or initial_fact.get("event_type") != "generation.export.requested":
                    raise VerificationFailed("created task/outbox event is not a database export request")
                if initial_fact.get("plan_id") is None or not initial_fact.get("plan_handle"):
                    raise VerificationFailed("database export plan was not durably registered")
                handle = str(initial_fact["plan_handle"])
                owner_user_id = int(initial_fact["owner_user_id"])
                conversation_id = int(initial_fact["conversation_id"])
                if conversation_id != int(args.conversation_id):
                    raise VerificationFailed("task conversation owner does not match the requested conversation")
                event_id = int(initial_fact["event_id"])
                observed_stream_id = _redis_find_event(redis_client, args.redis_stream, event_id)
                result["queue_publish_observed_immediately"] = observed_stream_id is not None
                if observed_stream_id:
                    result["queue_message_id"] = observed_stream_id

                if not args.no_owner_probe:
                    result["owner_stream_probe"] = _owner_stream_probe(
                        args, handle, owner_user_id, conversation_id
                    )
                else:
                    result["owner_stream_probe"] = {"skipped": True}

                deadline = time.monotonic() + args.timeout_seconds
                terminal: dict[str, Any] | None = None
                while time.monotonic() < deadline:
                    poll = client.get(f"/api/v1/generation-tasks/{task_id}")
                    if poll.status_code >= 500:
                        raise VerificationBlocked(f"API generation-task poll returned HTTP {poll.status_code}")
                    if poll.status_code != 200:
                        raise VerificationFailed(f"API generation-task poll returned HTTP {poll.status_code}")
                    polled = _parse_api_task(poll.json())
                    result["last_poll"] = _safe_api_status(polled)
                    status = polled.get("status")
                    if status in {"succeeded", "failed", "cancelled"}:
                        terminal = polled
                        break
                    time.sleep(max(0.05, args.poll_seconds))
                if terminal is None:
                    raise VerificationBlocked("real API/queue/Worker chain did not reach a terminal task within timeout")
                if terminal.get("status") != "succeeded" or terminal.get("complete") is not True:
                    raise VerificationFailed(f"real export task ended as {terminal.get('status')}: {terminal.get('error')}")
                final_fact = _task_fact(app_connection, task_id)
                _assert_durable_boundary(final_fact)
                result["final_fact"] = _safe_fact(final_fact)
                if final_fact.get("event_status") != "published":
                    raise VerificationFailed("generation export outbox event was not published/acknowledged")
                if final_fact.get("generated_file_id") is None or final_fact.get("file_id") is None:
                    raise VerificationFailed("succeeded task has no generated file/storage object")
                if final_fact.get("generation_kind") != "database_export" or final_fact.get("object_type") != "export":
                    raise VerificationFailed("generated file/storage object kind is not database_export/export")
                if final_fact.get("object_status") != "available":
                    raise VerificationFailed("generated storage object is not available")
                if final_fact.get("plan_id") is not None:
                    raise VerificationFailed("database export plan was not cleaned up after completion")
                exported_rows = int(final_fact.get("file_exported_rows") or 0)
                if args.scope == "all_matching" and exported_rows != EXPECTED_BUSINESS_ROWS:
                    raise VerificationFailed("all_matching export did not contain all 235 fixture rows")
                if args.scope == "first_n" and exported_rows != int(args.row_limit):
                    raise VerificationFailed("first_n export row count does not match the requested limit")
                if args.scope == "current_page" and not 1 <= exported_rows <= EXPECTED_BUSINESS_ROWS:
                    raise VerificationFailed("current_page export row count is outside the fixture range")

                file_id = int(final_fact["generated_file_id"])
                expected_sha = str(final_fact.get("file_sha256"))
                expected_size = int(final_fact.get("file_size_bytes") or 0)
                output_path = evidence_dir / f"generated-file-{file_id}.xlsx"
                result["download"] = _download_and_verify_file(
                    args,
                    file_id=file_id,
                    expected_sha256=expected_sha,
                    expected_size=expected_size,
                    output_path=output_path,
                )
                result["minio"] = _minio_stat(
                    args,
                    bucket=str(final_fact.get("bucket_name")),
                    object_key=str(final_fact.get("object_key")),
                )
                if int(result["minio"]["size_bytes"]) != expected_size:
                    raise VerificationFailed("MinIO object size does not match GeneratedFile")
                result["redis_after"] = _redis_snapshot(redis_client, args.redis_stream, args.redis_group)
                result["queue_event_id"] = event_id
                result["queue_event_status"] = final_fact.get("event_status")
                result["pids_after"] = _pid_snapshot(manifest)
                if not all(result["pids_after"].values()):
                    raise VerificationFailed("an API/Dispatcher/Worker/MCP process exited during the real chain")
                result["status"] = "passed"
        finally:
            app_connection.close()
    except VerificationBlocked as exc:
        result["status"] = "blocked"
        result["reason"] = str(exc)
    except VerificationFailed as exc:
        result["status"] = "failed"
        result["reason"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - final fail-closed evidence boundary
        result["status"] = "failed"
        result["reason"] = f"unexpected {type(exc).__name__}"
    finally:
        result["finished_at"] = datetime.now(UTC).isoformat()
        try:
            write_result()
        except OSError as exc:
            print(f"cannot write evidence: {exc}", file=sys.stderr)
            return 2

    print(json.dumps({"status": result["status"], "evidence": str(evidence_path)}, ensure_ascii=False))
    return {"passed": 0, "failed": 1, "blocked": 2}.get(str(result["status"]), 2)


if __name__ == "__main__":
    raise SystemExit(main())
