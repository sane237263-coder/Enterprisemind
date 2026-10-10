"""Prepare private local runtime files without changing existing credentials."""

import argparse
import json
import secrets
from pathlib import Path

import yaml


def prepare(root: Path, username: str) -> None:
    work = root / ".local/runtime"
    private = work / "secrets"
    private.mkdir(parents=True, exist_ok=True)
    for name in (
        "postgres_migrator_password",
        "postgres_app_password",
        "postgres_backup_password",
        "postgres_checkpoint_password",
        "bootstrap_admin_password",
        "redis_app_password",
        "redis_admin_password",
        "minio_root_secret",
        "minio_app_secret",
        "token_signing_key",
        "converter_token",
        "database_mcp_token",
        "database_readonly_password",
    ):
        path = private / name
        if not path.exists():
            with path.open("x", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(36))
    for name, prefix in (("minio_root_access", "emlocalroot"), ("minio_app_access", "emlocalapp")):
        path = private / name
        if not path.exists():
            with path.open("x", encoding="utf-8") as handle:
                handle.write(prefix + secrets.token_hex(6))
    acl = private / "redis.acl"
    if not acl.exists():
        app_password = (private / "redis_app_password").read_text(encoding="utf-8")
        admin_password = (private / "redis_admin_password").read_text(encoding="utf-8")
        acl.write_text(
            "user default off\n"
            f"user local_admin on >{admin_password} ~* &* +@all\n"
            f"user enterprise_mind_app on >{app_password} "
            "~enterprise-mind:* &enterprise-mind:* +ping +xgroup +xadd +xreadgroup "
            "+xautoclaim +xack +xdel +xpending +xrange +xlen +publish +subscribe "
            "+unsubscribe +get +set +del +exists +expire +ttl\n",
            encoding="utf-8",
        )
    runtime = work / "runtime.json"
    if not runtime.exists():
        runtime.write_text(
            json.dumps({"project": "enterprise-mind-local", "admin_username": username,
                        "postgres": {"kind": "ubuntu-native", "port": 5433,
                                     "database": "enterprise_mind"}}, indent=2),
            encoding="utf-8",
        )
    application = work / "application.yaml"
    if not application.exists():
        data = yaml.safe_load((root / "config/application.yaml").read_text(encoding="utf-8"))
        data["api"]["workers"] = 1
        data["postgres"].update(
            api_pool_max_per_process=4, worker_pool_max=4, scheduler_pool_max=2
        )
        data["office_converter"] = {
            "base_url": "http://127.0.0.1:38002",
        }
        application.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    database = work / "database.yaml"
    if not database.exists():
        database.write_text("database_mcp: null\n", encoding="utf-8")
    print(json.dumps({"prepared": True, "directory": str(work), "credentials_printed": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-username", default="localadmin")
    options = parser.parse_args()
    prepare(Path(__file__).resolve().parents[1], options.admin_username)
