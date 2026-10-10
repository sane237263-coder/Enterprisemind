"""Read the isolated app database migration marker without changing it."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))


def read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"secret file is empty: {path}")
    return value


def main() -> int:
    from enterprise_mind.infrastructure.database import SCHEMA_REVISION

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25432)
    parser.add_argument("--database", default="em_stage1_test")
    parser.add_argument("--user", default="em_test_owner")
    parser.add_argument(
        "--password-file",
        type=Path,
        default=Path(".local/test-secrets/postgres_password"),
    )
    parser.add_argument("--script-head", default=SCHEMA_REVISION)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    with psycopg.connect(
        host=args.host,
        port=args.port,
        dbname=args.database,
        user=args.user,
        password=read_secret(args.password_file),
        connect_timeout=5,
        application_name="enterprise_mind_phase10_migration_probe",
    ) as connection:
        database, user, version = connection.execute(
            "SELECT current_database(), current_user, version_num FROM infra.alembic_version"
        ).fetchone()
    result = {
        "database": database,
        "user": user,
        "current": version,
        "script_head": args.script_head,
        "matches": version == args.script_head,
    }
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
