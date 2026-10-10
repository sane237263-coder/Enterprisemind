"""Probe the isolated Phase-10 Redis and MinIO identities.

The probe uses only the dedicated app identities for the queue/object round
trip.  Root MinIO credentials are used only by the existing storage bootstrap
helper against the explicitly named Phase-10 bucket.  Secrets and payload
contents are never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from minio import Minio
from redis import Redis
from redis.exceptions import ResponseError

from enterprise_mind.infrastructure.settings import ApplicationConfig, IngestionSettings, read_secret
from enterprise_mind.ingestion.storage import ObjectStorage, initialize_storage


def secret(path: Path) -> str:
    return read_secret(path).get_secret_value()


def probe_redis(*, host: str, port: int, password_file: Path, stream: str) -> dict[str, object]:
    queue = Redis(
        host=host,
        port=port,
        username="enterprise_mind_app",
        password=secret(password_file),
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    group = "phase10-probe"
    consumer = "phase10-probe-consumer"
    try:
        pong = bool(queue.ping())
        try:
            queue.xgroup_create(stream, group, id="0", mkstream=True)
            group_created = True
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise
            # The exact stream/group belongs to this run and can be safely
            # reused after an interrupted probe.
            group_created = False
        message_id = queue.xadd(stream, {"event_id": "phase10-probe", "kind": "readiness"})
        messages = queue.xreadgroup(group, consumer, {stream: ">"}, count=1, block=1000)
        observed = sum(len(items) for _, items in messages)
        acknowledged = bool(queue.xack(stream, group, message_id))
        removed = int(queue.xdel(stream, message_id))
        return {
            "endpoint": f"{host}:{port}",
            "identity": "enterprise_mind_app",
            "ping": pong,
            "group_created": group_created,
            "group_reused": not group_created,
            "message_observed": observed == 1,
            "acknowledged": acknowledged,
            "message_removed": removed == 1,
            "stream": stream,
        }
    finally:
        queue.close()


def probe_minio(
    *,
    endpoint: str,
    bucket: str,
    app_access_file: Path,
    app_secret_file: Path,
    root_access_file: Path,
    root_secret_file: Path,
    object_key: str,
) -> dict[str, object]:
    settings = IngestionSettings(
        minio_endpoint=endpoint,
        minio_bucket=bucket,
        minio_access_key_file=app_access_file,
        minio_secret_key_file=app_secret_file,
        minio_secure=False,
        redis_host="127.0.0.1",
        redis_password_file=app_secret_file,
        redis_stream="enterprise-mind:phase10:minio-bootstrap",
    )
    config = ApplicationConfig().storage
    initialize_storage(settings, config, root_access_file, root_secret_file)
    storage = ObjectStorage(settings, config)
    payload = b"phase10-storage-probe\n"
    digest = hashlib.sha256(payload).hexdigest()
    storage.upload(object_key, io.BytesIO(payload), "text/plain")
    storage.verify(object_key, len(payload), digest)
    downloaded = b"".join(storage.blocks(object_key))
    read_matches = downloaded == payload
    storage.remove(object_key)
    root = Minio(
        endpoint,
        access_key=secret(root_access_file),
        secret_key=secret(root_secret_file),
        secure=False,
    )
    remaining = any(root.list_objects(bucket, prefix=object_key, recursive=True))
    return {
        "endpoint": endpoint,
        "bucket": bucket,
        "app_identity": "configured_minio_app",
        "bucket_check": True,
        "upload_verify": True,
        "download_matches": read_matches,
        "probe_object_removed": not remaining,
        "object_key_prefix": object_key.rsplit("/", 1)[0],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--redis-port", type=int, default=26379)
    parser.add_argument("--minio-port", type=int, default=29000)
    parser.add_argument("--secrets-dir", type=Path, default=ROOT / ".local" / "test-secrets")
    parser.add_argument("--app-access-file", type=Path)
    parser.add_argument("--app-secret-file", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if not args.run_id or any(char in args.run_id for char in "\\/:\r\n"):
        raise SystemExit("run-id must be a path-safe identity")
    secrets = args.secrets_dir
    app_access_file = args.app_access_file or secrets / "minio_app_access"
    app_secret_file = args.app_secret_file or secrets / "minio_app_secret"
    bucket = "em-p10-20260921-lm01-phase10"
    stream = f"enterprise-mind:phase10:{args.run_id}"
    object_key = f"phase10/{args.run_id}/storage-probe.txt"
    result = {
        "run_id": args.run_id,
        "redis": probe_redis(
            host="127.0.0.1",
            port=args.redis_port,
            password_file=secrets / "redis_password",
            stream=stream,
        ),
        "minio": probe_minio(
            endpoint=f"127.0.0.1:{args.minio_port}",
            bucket=bucket,
            app_access_file=app_access_file,
            app_secret_file=app_secret_file,
            root_access_file=secrets / "minio_root_access",
            root_secret_file=secrets / "minio_root_secret",
            object_key=object_key,
        ),
    }
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
