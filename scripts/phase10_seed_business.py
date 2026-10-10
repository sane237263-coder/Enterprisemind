"""Seed the isolated Phase-10 business database with the fixed 235-row fixture.

The script is intentionally fail-closed.  It only accepts the dedicated
``em_business_test`` database, never drops or truncates objects, refuses an
unmarked pre-existing fixture, creates a separate read-only MCP role, and
records only counts, identities and a fixture hash.  Passwords are read from
controlled files and are never printed or placed in evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

EXPECTED_DATABASE = "em_business_test"
EXPECTED_PROJECT = "em-p10-20260921-lm01"
EXPECTED_HOST = "127.0.0.1"
EXPECTED_PORT = 25433
SCHEMA = "business"
TABLE = "phase10_orders"
VIEW = "phase10_duplicate_view"
META_TABLE = "phase10_fixture_meta"
EXPECTED_COUNT = 235
SEED_VERSION = "phase10-235-v1"


def read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"secret file is empty: {path}")
    return value


def rows() -> list[tuple[int, str, str, date, Decimal, str | None, int]]:
    values: list[tuple[int, str, str, date, Decimal, str | None, int]] = []
    origin = date(2026, 1, 1)
    for identifier in range(1, EXPECTED_COUNT + 1):
        category = f"category-{identifier % 7:02d}"
        occurred = origin + timedelta(days=identifier % 31)
        amount = (Decimal(identifier * 37 % 10000) / Decimal(100)).quantize(
            Decimal("0.01")
        )
        note: str | None
        if identifier % 17 == 0:
            note = None
        elif identifier == 23:
            note = "=phase10-safe-text"
        else:
            note = f"fixture-note-{identifier:03d}"
        values.append(
            (
                identifier,
                f"{identifier:06d}",
                category,
                occurred,
                amount,
                note,
                identifier % 11,
            )
        )
    return values


def fixture_hash(
    values: list[tuple[int, str, str, date, Decimal, str | None, int]],
) -> str:
    payload = "\n".join(
        "\t".join(
            (
                str(identifier),
                code,
                category,
                occurred.isoformat(),
                format(amount, "f"),
                "<NULL>" if note is None else note,
                str(group),
            )
        )
        for identifier, code, category, occurred, amount, note, group in values
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def connect(
    *, host: str, port: int, database: str, user: str, password: str
) -> psycopg.Connection:
    return psycopg.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password,
        connect_timeout=5,
        application_name="enterprise_mind_phase10_fixture",
    )


def verify_compose_identity(*, project: str, host: str, port: int) -> None:
    """Refuse writes unless the dedicated Compose service owns this endpoint."""

    if project != EXPECTED_PROJECT or host != EXPECTED_HOST or port != EXPECTED_PORT:
        raise RuntimeError("refusing a non-Phase-10 project, host or business port")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,62}", project):
        raise RuntimeError("invalid Compose project identity")
    result = subprocess.run(
        [
            "wsl.exe",
            "-d",
            "Ubuntu",
            "--",
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=business-postgresql",
            "--format",
            '{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.Ports}}',
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError("cannot verify the dedicated Docker Compose identity")
    expected_name = f"{project}-business-postgresql-1"
    records = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    expected_port = f"{host}:{port}->5432/tcp"
    if records != [f"{expected_name}|{project}|{expected_port}"]:
        raise RuntimeError("business database container/project/port identity mismatch")


def ensure_database_identity(connection: psycopg.Connection, expected: str) -> None:
    actual = connection.execute("SELECT current_database()").fetchone()
    if actual is None or actual[0] != expected:
        raise RuntimeError(
            f"refusing unexpected database identity: {actual[0] if actual else None}"
        )


def ensure_role(connection: psycopg.Connection, role: str, password: str) -> str:
    if not role.startswith("em_p10_"):
        raise ValueError("readonly role must use the em_p10_ prefix")
    existing = connection.execute(
        """
        SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolinherit
        FROM pg_catalog.pg_roles WHERE rolname = %s
        """,
        (role,),
    ).fetchone()
    if existing is None:
        connection.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD {}"
            ).format(sql.Identifier(role), sql.Literal(password)),
        )
        action = "created"
    else:
        if any(bool(item) for item in existing):
            raise RuntimeError("existing readonly role has an unsafe role attribute")
        action = "reused"
    connection.execute(
        sql.SQL("ALTER ROLE {} SET default_transaction_read_only = 'on'").format(
            sql.Identifier(role)
        )
    )
    return action


def preflight_fixture_state(
    connection: psycopg.Connection, run_id: str
) -> list[tuple[Any, ...]]:
    """Return same-run markers; reject any unowned existing business schema."""

    schema_exists = bool(
        connection.execute(
            "SELECT to_regnamespace(%s) IS NOT NULL", (SCHEMA,)
        ).fetchone()[0]
    )
    if not schema_exists:
        return []
    objects = connection.execute(
        """
        SELECT to_regclass('business.phase10_orders'),
               to_regclass('business.phase10_duplicate_view'),
               to_regclass('business.phase10_fixture_meta')
        """
    ).fetchone()
    if objects is None or objects[2] is None:
        raise RuntimeError("refusing pre-existing unmarked business schema")
    markers = connection.execute(
        sql.SQL(
            "SELECT run_id, seed_version, row_count, fixture_hash FROM {}.{}"
        ).format(sql.Identifier(SCHEMA), sql.Identifier(META_TABLE))
    ).fetchall()
    if not markers or any(marker[0] != run_id for marker in markers):
        raise RuntimeError("refusing pre-existing fixture without this run marker")
    return markers


def ensure_fixture(
    connection: psycopg.Connection,
    *,
    run_id: str,
    readonly_role: str,
    values: list[tuple[int, str, str, date, Decimal, str | None, int]],
) -> dict[str, Any]:
    markers = preflight_fixture_state(connection, run_id)
    connection.execute(
        sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION CURRENT_USER").format(
            sql.Identifier(SCHEMA)
        )
    )
    connection.execute(
        sql.SQL(
            """CREATE TABLE IF NOT EXISTS {}.{} (
                id bigint PRIMARY KEY,
                order_code varchar(32) NOT NULL,
                category varchar(64) NOT NULL,
                occurred_on date NOT NULL,
                amount numeric(20, 2) NOT NULL,
                nullable_note text,
                duplicate_group integer NOT NULL
            )"""
        ).format(sql.Identifier(SCHEMA), sql.Identifier(TABLE))
    )
    connection.execute(
        sql.SQL(
            """CREATE TABLE IF NOT EXISTS {}.{} (
                run_id varchar(128) PRIMARY KEY,
                seed_version varchar(64) NOT NULL,
                row_count integer NOT NULL,
                fixture_hash varchar(64) NOT NULL
            )"""
        ).format(sql.Identifier(SCHEMA), sql.Identifier(META_TABLE))
    )
    existing_count = connection.execute(
        sql.SQL("SELECT count(*) FROM {}.{}").format(
            sql.Identifier(SCHEMA), sql.Identifier(TABLE)
        )
    ).fetchone()[0]
    expected_hash = fixture_hash(values)
    if existing_count not in (0, EXPECTED_COUNT):
        raise RuntimeError(
            f"refusing pre-existing unmarked row count: {existing_count}"
        )
    if existing_count == 0:
        with connection.cursor() as cursor:
            cursor.executemany(
                sql.SQL(
                    "INSERT INTO {}.{} "
                    "(id, order_code, category, occurred_on, amount, nullable_note, duplicate_group) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
                ).format(sql.Identifier(SCHEMA), sql.Identifier(TABLE)),
                values,
            )
    observed = connection.execute(
        sql.SQL(
            "SELECT id, order_code, category, occurred_on, amount, nullable_note, duplicate_group "
            "FROM {}.{} ORDER BY id"
        ).format(sql.Identifier(SCHEMA), sql.Identifier(TABLE))
    ).fetchall()
    normalized = [
        (
            int(identifier),
            str(code),
            str(category),
            occurred,
            Decimal(amount),
            note,
            int(group),
        )
        for identifier, code, category, occurred, amount, note, group in observed
    ]
    observed_hash = fixture_hash(normalized)
    if len(normalized) != EXPECTED_COUNT or observed_hash != expected_hash:
        raise RuntimeError("fixture rows do not match the fixed Phase-10 seed")
    connection.execute(
        sql.SQL(
            "CREATE OR REPLACE VIEW {}.{} AS SELECT category, amount, duplicate_group, nullable_note FROM {}.{}"
        ).format(
            sql.Identifier(SCHEMA),
            sql.Identifier(VIEW),
            sql.Identifier(SCHEMA),
            sql.Identifier(TABLE),
        )
    )
    connection.execute(
        sql.SQL(
            "INSERT INTO {}.{} (run_id, seed_version, row_count, fixture_hash) VALUES (%s, %s, %s, %s) ON CONFLICT (run_id) DO UPDATE SET seed_version=EXCLUDED.seed_version, row_count=EXCLUDED.row_count, fixture_hash=EXCLUDED.fixture_hash"
        ).format(sql.Identifier(SCHEMA), sql.Identifier(META_TABLE)),
        (run_id, SEED_VERSION, EXPECTED_COUNT, expected_hash),
    )
    # Revoke broad defaults before granting the two explicitly whitelisted
    # read objects.  These statements target only this isolated database.
    connection.execute(
        sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(
            sql.Identifier(EXPECTED_DATABASE)
        )
    )
    connection.execute(
        sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(SCHEMA))
    )
    connection.execute(
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC").format(
            sql.Identifier(SCHEMA)
        )
    )
    connection.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(EXPECTED_DATABASE), sql.Identifier(readonly_role)
        )
    )
    connection.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(SCHEMA), sql.Identifier(readonly_role)
        )
    )
    connection.execute(
        sql.SQL("GRANT SELECT ON TABLE {}.{}, {}.{} TO {}").format(
            sql.Identifier(SCHEMA),
            sql.Identifier(TABLE),
            sql.Identifier(SCHEMA),
            sql.Identifier(VIEW),
            sql.Identifier(readonly_role),
        )
    )
    connection.execute(
        sql.SQL("REVOKE ALL ON TABLE {}.{} FROM {}").format(
            sql.Identifier(SCHEMA),
            sql.Identifier(META_TABLE),
            sql.Identifier(readonly_role),
        )
    )
    connection.execute(
        sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(
            sql.Identifier(SCHEMA), sql.Identifier(readonly_role)
        )
    )
    connection.commit()
    return {
        "database": EXPECTED_DATABASE,
        "schema": SCHEMA,
        "table": TABLE,
        "view": VIEW,
        "readonly_role": readonly_role,
        "seed_version": SEED_VERSION,
        "row_count": EXPECTED_COUNT,
        "fixture_sha256": expected_hash,
        "ownership_run_id": run_id,
        "existing_marker_count": len(markers),
    }


def verify_readonly(
    *, host: str, port: int, database: str, role: str, password: str
) -> dict[str, Any]:
    with connect(
        host=host, port=port, database=database, user=role, password=password
    ) as connection:
        ensure_database_identity(connection, EXPECTED_DATABASE)
        count = connection.execute(
            sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(SCHEMA), sql.Identifier(TABLE)
            )
        ).fetchone()[0]
        role_flags = connection.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit FROM pg_catalog.pg_roles WHERE rolname = current_user"
        ).fetchone()
        write_blocked = False
        try:
            connection.execute(
                sql.SQL("CREATE TABLE {}.phase10_write_probe (id integer)").format(
                    sql.Identifier(SCHEMA)
                )
            )
        except (
            psycopg.errors.InsufficientPrivilege,
            psycopg.errors.ReadOnlySqlTransaction,
        ):
            connection.rollback()
            write_blocked = True
        else:
            connection.rollback()
        if not write_blocked:
            raise RuntimeError("readonly account unexpectedly created a table")
        return {
            "connected_user": role,
            "database": database,
            "visible_fixture_rows": int(count),
            "role_flags_all_false": bool(
                role_flags and not any(bool(value) for value in role_flags)
            ),
            "write_probe_blocked": write_blocked,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--project-name",
        default=os.environ.get("EM_P10_COMPOSE_PROJECT", EXPECTED_PROJECT),
    )
    parser.add_argument(
        "--host", default=os.environ.get("EM_P10_BUSINESS_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("EM_P10_BUSINESS_PORT", "25433"))
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("EM_P10_BUSINESS_DATABASE", EXPECTED_DATABASE),
    )
    parser.add_argument(
        "--owner-user",
        default=os.environ.get("EM_P10_BUSINESS_OWNER", "em_business_owner"),
    )
    parser.add_argument(
        "--owner-password-file",
        type=Path,
        default=Path(
            os.environ.get(
                "EM_P10_BUSINESS_PASSWORD_FILE", ".local/test-secrets/business_password"
            )
        ),
    )
    parser.add_argument(
        "--readonly-user",
        default=os.environ.get(
            "EM_P10_BUSINESS_READONLY_USER", "em_p10_business_readonly"
        ),
    )
    parser.add_argument(
        "--readonly-password-file",
        type=Path,
        default=Path(
            os.environ.get(
                "EM_P10_BUSINESS_READONLY_PASSWORD_FILE",
                ".local/test-secrets/phase10_business_readonly_password",
            )
        ),
    )
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.database != EXPECTED_DATABASE:
        raise SystemExit(f"refusing database other than {EXPECTED_DATABASE}")
    if args.owner_user != "em_business_owner":
        raise SystemExit("refusing a non-Phase-10 business maintenance identity")
    verify_compose_identity(project=args.project_name, host=args.host, port=args.port)
    if not args.run_id or any(char in args.run_id for char in "\\/:\r\n"):
        raise SystemExit("run-id must be a path-safe ownership marker")
    owner_password = read_secret(args.owner_password_file)
    readonly_password = read_secret(args.readonly_password_file)
    expected = rows()
    with connect(
        host=args.host,
        port=args.port,
        database=args.database,
        user=args.owner_user,
        password=owner_password,
    ) as owner:
        ensure_database_identity(owner, EXPECTED_DATABASE)
        # Preflight the ownership marker before any role, table or grant write.
        # An unmarked schema is never reused.
        preflight_fixture_state(owner, args.run_id)
        role_action = ensure_role(owner, args.readonly_user, readonly_password)
        fixture = ensure_fixture(
            owner,
            run_id=args.run_id,
            readonly_role=args.readonly_user,
            values=expected,
        )
    access = verify_readonly(
        host=args.host,
        port=args.port,
        database=args.database,
        role=args.readonly_user,
        password=readonly_password,
    )
    result: dict[str, Any] = {
        "run_id": args.run_id,
        "host": args.host,
        "port": args.port,
        "role_action": role_action,
        "fixture": fixture,
        "readonly_probe": access,
    }
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, psycopg.Error, RuntimeError, ValueError) as error:
        print(
            f"phase10 fixture failed: {type(error).__name__}: {error}", file=sys.stderr
        )
        raise SystemExit(1) from None
