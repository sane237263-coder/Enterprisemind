"""Inspect only the Phase-10 MinIO identity and policy metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from minio import Minio
from minio.minioadmin import MinioAdmin
from minio.credentials.providers import StaticProvider


def secret(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def call_status(function) -> dict[str, object]:
    try:
        value = function()
        if isinstance(value, bool):
            return {"ok": value}
        return {"ok": True, "type": type(value).__name__}
    except Exception as error:  # noqa: BLE001 - metadata probe boundary
        return {"ok": False, "exception_type": type(error).__name__, "message": str(error)}


def user_metadata(function) -> dict[str, object]:
    try:
        value = json.loads(function())
        return {
            "ok": True,
            "user_status": value.get("userStatus"),
            "policy_name": value.get("policyName"),
        }
    except Exception as error:  # noqa: BLE001 - metadata probe boundary
        return {"ok": False, "exception_type": type(error).__name__, "message": str(error)}


def policy_metadata(function) -> dict[str, object]:
    try:
        value = json.loads(function())
        policy = value if isinstance(value, dict) and "Statement" in value else None
        if policy is None and isinstance(value, dict):
            policy = value.get("policy") or value.get("Policy")
        if isinstance(policy, str):
            policy = json.loads(policy)
        statements = policy.get("Statement", []) if isinstance(policy, dict) else []
        actions: list[str] = []
        resources: list[str] = []
        for statement in statements:
            if isinstance(statement, dict):
                actions.extend(str(item) for item in statement.get("Action", []))
                resources.extend(str(item) for item in statement.get("Resource", []))
        return {
            "ok": True,
            "top_level_keys": sorted(value) if isinstance(value, dict) else [],
            "policy_keys": sorted(policy) if isinstance(policy, dict) else [],
            "statement_count": len(statements),
            "actions": sorted(set(actions)),
            "resources": sorted(set(resources)),
        }
    except Exception as error:  # noqa: BLE001 - metadata probe boundary
        return {"ok": False, "exception_type": type(error).__name__, "message": str(error)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="127.0.0.1:29000")
    parser.add_argument("--bucket", default="em-p10-20260921-lm01-phase10")
    parser.add_argument("--secrets-dir", type=Path, default=Path(".local/test-secrets"))
    parser.add_argument("--app-access-file", type=Path)
    parser.add_argument("--app-secret-file", type=Path)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    app_access = secret(args.app_access_file or args.secrets_dir / "minio_app_access")
    app_secret = secret(args.app_secret_file or args.secrets_dir / "minio_app_secret")
    root_access = secret(args.secrets_dir / "minio_root_access")
    root_secret = secret(args.secrets_dir / "minio_root_secret")
    app = Minio(args.endpoint, access_key=app_access, secret_key=app_secret, secure=False)
    root = Minio(args.endpoint, access_key=root_access, secret_key=root_secret, secure=False)
    admin = MinioAdmin(
        endpoint=args.endpoint,
        credentials=StaticProvider(root_access, root_secret),
        secure=False,
    )
    policy_name = "enterprise-mind-bucket-" + hashlib.sha256(args.bucket.encode("utf-8")).hexdigest()[:16]
    result = {
        "endpoint": args.endpoint,
        "bucket": args.bucket,
        "app_access_sha256": hashlib.sha256(app_access.encode()).hexdigest(),
        "root_bucket_exists": call_status(lambda: root.bucket_exists(args.bucket)),
        "app_bucket_exists": call_status(lambda: app.bucket_exists(args.bucket)),
        "app_list_objects_prefix": call_status(
            lambda: list(app.list_objects(args.bucket, prefix="phase10/", recursive=True))
        ),
        "admin_user_info": user_metadata(lambda: admin.user_info(app_access)),
        "policy_name": policy_name,
        "admin_policy_info": policy_metadata(lambda: admin.policy_info(policy_name)),
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
