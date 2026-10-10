"""Run local backend checks with the interpreter from the project environment."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument(
    "--integration",
    action="store_true",
    help="Require the dedicated PostgreSQL fixture",
)
parser.add_argument(
    "--ingestion",
    action="store_true",
    help="Include dedicated Redis and MinIO fixtures",
)
parser.add_argument(
    "--database",
    action="store_true",
    help="Require the isolated Database MCP business PostgreSQL fixture",
)
parser.add_argument(
    "--basetemp",
    help="Dedicated temporary test directory, useful with Windows ACLs",
)
args = parser.parse_args()
if args.ingestion:
    args.integration = True
    os.environ["EM_TEST_INGESTION"] = "1"
if args.integration and not os.environ.get("EM_TEST_POSTGRES_PASSWORD_FILE"):
    parser.error(
        "--integration requires EM_TEST_POSTGRES_PASSWORD_FILE; tests must not silently skip"
    )
if args.database:
    args.integration = True
    if not os.environ.get("EM_TEST_POSTGRES_PASSWORD_FILE") or not os.environ.get(
        "EM_TEST_BUSINESS_PASSWORD_FILE"
    ):
        parser.error(
            "--database requires both EM_TEST_POSTGRES_PASSWORD_FILE and EM_TEST_BUSINESS_PASSWORD_FILE"
        )

backend = Path(__file__).resolve().parents[1] / "backend"
commands = [
    ["ruff", "format", "--check", "src", "tests", "migrations"],
    ["ruff", "check", "src", "tests", "migrations"],
    ["mypy", "src"],
    ["pytest", "-q", "--tb=short", "-p", "no:cacheprovider"],
]
if not args.integration:
    commands[-1].extend(["-m", "not integration"])
if args.basetemp:
    commands[-1].extend(["--basetemp", str(Path(args.basetemp).resolve())])
for command in commands:
    result = subprocess.run([sys.executable, "-m", *command], cwd=backend, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
