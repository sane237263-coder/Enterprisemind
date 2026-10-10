"""Provision only the EM application database on Ubuntu PG via local peer auth.

Run as the postgres OS user, with Ubuntu Python; requires no administrator
password and never changes credentials or permissions in any other database.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".local/runtime/secrets"
ROLES = {
    "enterprise_mind_owner": None,
    "enterprise_mind_migrator": "postgres_migrator_password",
    "enterprise_mind_app": "postgres_app_password",
    "enterprise_mind_backup": "postgres_backup_password",
    "enterprise_mind_checkpoint": "postgres_checkpoint_password",
}


def query(statement: str, database: str = "postgres") -> str:
    outcome = subprocess.run(
        ["/usr/bin/psql", "-X", "-qAt", "-p", "5433", "-d", database, "-v", "ON_ERROR_STOP=1"],
        input=statement, text=True, capture_output=True, timeout=40, check=False,
    )
    if outcome.returncode:
        raise RuntimeError("Ubuntu EM provisioning failed; no SQL or credentials printed")
    return outcome.stdout.strip()


def verifier(password: str) -> str:
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 4096)
    stored = hashlib.sha256(hmac.digest(salted, b"Client Key", "sha256")).digest()
    server = hmac.digest(salted, b"Server Key", "sha256")
    enc = lambda data: base64.b64encode(data).decode("ascii")
    return f"SCRAM-SHA-256$4096:{enc(salt)}${enc(stored)}:{enc(server)}"


def main() -> None:
    if os.geteuid() == 0:
        raise RuntimeError("run as postgres OS user, not root")
    if query("SELECT current_user") != "postgres":
        raise RuntimeError("local peer identity mismatch")
    # Refuse unexpected pre-existing role properties; never reset existing passwords.
    statements = []
    for role, filename in ROLES.items():
        prior = query(f"SELECT rolcanlogin::int||','||rolsuper::int||','||rolcreatedb::int||','||rolcreaterole::int||','||rolreplication::int||','||rolbypassrls::int||','||rolinherit::int FROM pg_roles WHERE rolname='{role}'")
        expected = ("0" if filename is None else "1") + ",0,0,0,0,0,0"
        if prior:
            if prior != expected:
                raise RuntimeError("existing EM role properties mismatch")
            continue
        mode = "NOLOGIN" if filename is None else "LOGIN"
        password_clause = ""
        if filename:
            password = (PRIVATE / filename).read_text(encoding="utf-8").strip()
            if not password or any(ch in password for ch in "\x00\n\r"):
                raise RuntimeError("invalid local Secret")
            password_clause = f" PASSWORD '{verifier(password)}'"
        statements.append(f"CREATE ROLE {role} {mode} NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS{password_clause};")
    statements.append("GRANT enterprise_mind_owner TO enterprise_mind_migrator WITH INHERIT FALSE, SET TRUE;")
    query("BEGIN; SET LOCAL log_statement='none'; SET LOCAL log_min_duration_statement=-1; SET LOCAL log_min_error_statement='panic'; " + " ".join(statements) + " COMMIT;")
    existing = query("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='enterprise_mind'")
    if existing and existing != "enterprise_mind_owner":
        raise RuntimeError("existing application database owner mismatch")
    if not existing:
        query("CREATE DATABASE enterprise_mind OWNER enterprise_mind_owner;")
    query("""
BEGIN;
REVOKE ALL ON DATABASE enterprise_mind FROM PUBLIC;
GRANT CONNECT ON DATABASE enterprise_mind TO enterprise_mind_migrator,enterprise_mind_app,enterprise_mind_backup,enterprise_mind_checkpoint;
REVOKE CREATE ON SCHEMA public FROM PUBLIC,enterprise_mind_app,enterprise_mind_backup;
CREATE EXTENSION IF NOT EXISTS vector VERSION '0.8.6';
CREATE EXTENSION IF NOT EXISTS pg_trgm;
COMMIT;
""", "enterprise_mind")
    report = {"status": "provisioned", "cluster_port": 5433, "database": "enterprise_mind", "roles": list(ROLES), "existing_credentials_preserved": True}
    print(json.dumps(report))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - entry point must redact every credential-bearing error
        print(json.dumps({"status": "not_completed", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
