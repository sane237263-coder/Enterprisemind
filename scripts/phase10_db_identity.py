"""Read-only identity probe for the isolated Phase-10 business database.

The probe intentionally emits connection and role metadata only.  It never
prints passwords and never changes server state.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import psycopg


def read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"secret file is empty: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25433)
    parser.add_argument("--database", default="em_business_test")
    parser.add_argument("--user", default="em_business_owner")
    parser.add_argument(
        "--password-file", type=Path, default=Path(".local/test-secrets/business_password")
    )
    args = parser.parse_args()
    with psycopg.connect(
        host=args.host,
        port=args.port,
        dbname=args.database,
        user=args.user,
        password=read_secret(args.password_file),
        connect_timeout=5,
        application_name="enterprise_mind_phase10_identity_probe",
    ) as connection:
        row = connection.execute(
            """
            SELECT
              current_database(), current_user,
              current_setting('transaction_read_only'),
              current_setting('default_transaction_read_only'),
              pg_is_in_recovery(), current_setting('server_version'),
              r.rolsuper, r.rolcreatedb, r.rolcreaterole,
              r.rolreplication, r.rolbypassrls, r.rolinherit
            FROM pg_catalog.pg_roles AS r
            WHERE r.rolname = current_user
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("connected role metadata was not found")
        keys = (
            "database",
            "user",
            "transaction_read_only",
            "default_transaction_read_only",
            "in_recovery",
            "server_version",
            "rolsuper",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
            "rolinherit",
        )
        print(json.dumps(dict(zip(keys, row)), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
