"""Run the P10-08 provision/migration probe on one explicitly owned test cluster.

The probe is deliberately fail-closed. It verifies the Compose container identity
before creating roles or a database, refuses an existing target database, and only
creates/removes the named probe table in the new target database.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import URL, create_engine, text
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND / "src"))
ROLES = (
    "enterprise_mind_owner",
    "enterprise_mind_migrator",
    "enterprise_mind_app",
    "enterprise_mind_backup",
    "enterprise_mind_checkpoint",
)
UNKNOWN_TABLE = "infra.p10_08_unknown_probe"


def _run_docker_inspect(container: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["wsl.exe", "-d", "Ubuntu", "--", "docker", "inspect", container],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("docker inspect failed")
    try:
        payload = json.loads(completed.stdout)
        return payload[0]
    except (json.JSONDecodeError, IndexError, TypeError) as exc:
        raise RuntimeError("docker inspect returned invalid metadata") from exc


def _verify_container(project: str, port: int) -> dict[str, str]:
    container = f"{project}-postgresql-1"
    inspected = _run_docker_inspect(container)
    labels = inspected.get("Config", {}).get("Labels", {})
    state = inspected.get("State", {})
    health = state.get("Health", {}).get("Status")
    published = (
        inspected.get("NetworkSettings", {}).get("Ports", {}).get("5432/tcp", [])
    )
    matches = [item for item in published if item.get("HostIp") == "127.0.0.1"]
    if (
        labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.service") != "postgresql"
        or state.get("Status") != "running"
        or health != "healthy"
        or not any(item.get("HostPort") == str(port) for item in matches)
    ):
        raise RuntimeError("Compose identity or port did not match the requested probe")
    return {
        "project": project,
        "service": "postgresql",
        "container": container,
        "container_id": str(inspected.get("Id", "")),
        "status": str(state.get("Status")),
        "health": str(health),
        "host_ip": "127.0.0.1",
        "host_port": str(port),
    }


def _url(*, user: str, password: str, host: str, port: int, database: str) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=host,
        port=port,
        database=database,
    )


def _write_secret(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secrets.token_urlsafe(32), encoding="utf-8")


def _target_exists(url: URL) -> bool:
    maintenance = create_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
    )
    try:
        with maintenance.connect() as connection:
            return bool(
                connection.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname=:name"),
                    {"name": url.database},
                )
            )
    finally:
        maintenance.dispose()


def _existing_roles(url: URL) -> set[str]:
    maintenance = create_engine(
        url.set(database="postgres"),
        hide_parameters=True,
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
    )
    try:
        with maintenance.connect() as connection:
            return set(
                connection.execute(
                    text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:roles)"),
                    {"roles": list(ROLES)},
                )
                .scalars()
                .all()
            )
    finally:
        maintenance.dispose()


def _role_security(engine) -> dict[str, dict[str, bool]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
                "rolreplication, rolbypassrls, rolinherit "
                "FROM pg_roles WHERE rolname = ANY(:roles) ORDER BY rolname"
            ),
            {"roles": list(ROLES)},
        ).mappings()
        return {
            str(row["rolname"]): {
                key: bool(row[key])
                for key in (
                    "rolcanlogin",
                    "rolsuper",
                    "rolcreatedb",
                    "rolcreaterole",
                    "rolreplication",
                    "rolbypassrls",
                    "rolinherit",
                )
            }
            for row in rows
        }


def _probe(args: argparse.Namespace) -> dict[str, Any]:
    from enterprise_mind.infrastructure.database import SCHEMA_REVISION
    from enterprise_mind.infrastructure.migrate import migrate
    from enterprise_mind.infrastructure.provision import (
        APP,
        BACKUP,
        CHECKPOINT,
        MIGRATOR,
        OWNER,
        TABLES,
        provision,
    )
    from enterprise_mind.infrastructure.settings import ApplicationConfig, read_secret

    identity = _verify_container(args.project, args.port)
    owner_secret = Path(args.owner_password_file).resolve()
    owner_password = read_secret(owner_secret).get_secret_value()
    owner_url = _url(
        user="em_test_owner",
        password=owner_password,
        host=args.host,
        port=args.port,
        database=args.database,
    )
    if not args.database.startswith("em_p1008_"):
        raise RuntimeError("target database must use the P10-08 probe namespace")
    if _target_exists(owner_url):
        raise RuntimeError("target database already exists; refusing to adopt it")
    existing_roles = _existing_roles(owner_url)
    if existing_roles and not args.role_secret_dir:
        raise RuntimeError(
            "deployment roles already exist; provide the original role Secret directory explicitly"
        )

    secret_dir = (
        Path(args.role_secret_dir).resolve()
        if args.role_secret_dir
        else Path(args.evidence).resolve() / "runtime" / "provision-secrets"
    )
    files: dict[str, Path] = {}
    for role in (MIGRATOR, APP, BACKUP, CHECKPOINT):
        path = secret_dir / role
        if args.role_secret_dir:
            if not path.is_file():
                raise RuntimeError("requested role secret file is missing")
        else:
            _write_secret(path)
        files[role] = path
    bootstrap_secret = (
        Path(args.evidence).resolve() / "runtime" / ("bootstrap-admin-" + args.database)
    )
    _write_secret(bootstrap_secret)
    old_username = os.environ.get("EM_BOOTSTRAP_ADMIN_USERNAME")
    old_password_file = os.environ.get("EM_BOOTSTRAP_ADMIN_PASSWORD_FILE")
    os.environ["EM_BOOTSTRAP_ADMIN_USERNAME"] = "p10-08-probe-admin"
    os.environ["EM_BOOTSTRAP_ADMIN_PASSWORD_FILE"] = str(bootstrap_secret)

    owner_engine = create_engine(owner_url, hide_parameters=True, poolclass=NullPool)
    try:
        provision(owner_url, files)
        provision(owner_url, {})
        migration_url = owner_url.set(
            username=MIGRATOR,
            password=read_secret(files[MIGRATOR]).get_secret_value(),
        )
        migration_first = migrate(migration_url, ApplicationConfig(), BACKEND)
        migration_second = migrate(migration_url, ApplicationConfig(), BACKEND)
        provision(owner_url, {})

        with owner_engine.connect() as connection:
            version = connection.scalar(
                text("SELECT version_num FROM infra.alembic_version")
            )
            missing_tables = [
                name
                for name in TABLES
                if name.startswith(
                    ("iam.", "knowledge.", "agent.", "integration.", "infra.")
                )
                and connection.scalar(text("SELECT to_regclass(:name)"), {"name": name})
                is None
            ]
            role_memberships = (
                connection.execute(
                    text(
                        "SELECT r.rolname, m.rolname AS member, a.admin_option "
                        "FROM pg_auth_members a "
                        "JOIN pg_roles r ON r.oid=a.roleid "
                        "JOIN pg_roles m ON m.oid=a.member "
                        "WHERE r.rolname = :owner AND m.rolname = :migrator"
                    ),
                    {"owner": OWNER, "migrator": MIGRATOR},
                )
                .mappings()
                .all()
            )
            role_security = _role_security(owner_engine)
        with owner_engine.begin() as connection:
            connection.execute(text(f"CREATE TABLE {UNKNOWN_TABLE} (id integer)"))

        unknown_rejected = False
        unknown_error = None
        try:
            provision(owner_url, {})
        except Exception as exc:  # noqa: BLE001 - record only the class, never SQL/secret data.
            unknown_rejected = exc.__class__.__name__ == "ConfigurationError"
            unknown_error = exc.__class__.__name__
        finally:
            with owner_engine.begin() as connection:
                connection.execute(text(f"DROP TABLE IF EXISTS {UNKNOWN_TABLE}"))
        if not unknown_rejected:
            raise RuntimeError(
                f"unknown object was not rejected ({unknown_error or 'no error'})"
            )

        app_url = owner_url.set(
            username=APP,
            password=read_secret(files[APP]).get_secret_value(),
        )
        backup_url = owner_url.set(
            username=BACKUP,
            password=read_secret(files[BACKUP]).get_secret_value(),
        )
        app_engine = create_engine(app_url, hide_parameters=True, poolclass=NullPool)
        backup_engine = create_engine(
            backup_url, hide_parameters=True, poolclass=NullPool
        )
        try:
            with app_engine.connect() as connection:
                app_write = bool(
                    connection.scalar(
                        text(
                            "SELECT has_table_privilege(current_user, "
                            "'agent.generation_tasks', 'SELECT,INSERT,UPDATE,DELETE')"
                        )
                    )
                )
            with backup_engine.connect() as connection:
                backup_select = bool(
                    connection.scalar(
                        text(
                            "SELECT has_table_privilege(current_user, "
                            "'agent.generation_tasks', 'SELECT')"
                        )
                    )
                )
                backup_insert = bool(
                    connection.scalar(
                        text(
                            "SELECT has_table_privilege(current_user, "
                            "'agent.generation_tasks', 'INSERT')"
                        )
                    )
                )
        finally:
            app_engine.dispose()
            backup_engine.dispose()
    finally:
        owner_engine.dispose()
        if old_username is None:
            os.environ.pop("EM_BOOTSTRAP_ADMIN_USERNAME", None)
        else:
            os.environ["EM_BOOTSTRAP_ADMIN_USERNAME"] = old_username
        if old_password_file is None:
            os.environ.pop("EM_BOOTSTRAP_ADMIN_PASSWORD_FILE", None)
        else:
            os.environ["EM_BOOTSTRAP_ADMIN_PASSWORD_FILE"] = old_password_file

    return {
        "status": "PASS",
        "identity": identity,
        "target_database": args.database,
        "migration": {
            "first_created_admin": bool(migration_first),
            "second_created_admin": bool(migration_second),
            "current": version,
            "expected": SCHEMA_REVISION,
            "matches": version == SCHEMA_REVISION,
        },
        "provision": {
            "idempotent_before_migration": True,
            "idempotent_after_migration": True,
            "missing_whitelisted_tables": missing_tables,
            "unknown_object_rejected": unknown_rejected,
            "existing_roles_before": sorted(existing_roles),
            "role_secret_mode": "reused_explicit"
            if args.role_secret_dir
            else "generated_empty_cluster",
        },
        "roles": {
            "security": role_security,
            "owner_to_migrator_membership": [
                {"member": row["member"], "admin_option": bool(row["admin_option"])}
                for row in role_memberships
            ],
            "app_write_on_generation_tasks": app_write,
            "backup_select_on_generation_tasks": backup_select,
            "backup_insert_on_generation_tasks": backup_insert,
        },
        "secrets": {
            "owner_password_file": str(owner_secret),
            "generated_role_secret_dir": str(secret_dir),
            "values_recorded": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--owner-password-file", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--role-secret-dir",
        type=Path,
        help="reuse role Secret files explicitly created by an earlier probe in this cluster",
    )
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    output = args.evidence / "provision-result.json"
    try:
        result = _probe(args)
    except Exception as exc:  # noqa: BLE001 - evidence must never include a secret.
        result = {
            "status": "FAILED",
            "error_type": exc.__class__.__name__,
            "error_detail": str(exc)[:240],
        }
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"status": result["status"], "evidence": str(output)}, ensure_ascii=False
        )
    )
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
