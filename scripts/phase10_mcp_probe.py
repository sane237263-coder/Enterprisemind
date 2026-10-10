"""Exercise the independent MCP HTTP boundary with the synthetic source."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from enterprise_mind.database.guard import SQLGuard
from enterprise_mind.database.mcp_client import DatabaseMCPClient


def read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"secret file is empty: {path}")
    return value


async def run(endpoint: str, token_file: Path) -> dict[str, object]:
    source = {
        "host": "127.0.0.1",
        "port": 25433,
        "database_name": "em_business_test",
        "ssl_mode": "disable",
        "secret_ref": "secret:phase10_business_readonly",
        "connection_options": {},
    }
    client = DatabaseMCPClient(endpoint, read_secret(token_file), source=source)
    try:
        catalog = await client.fetch_schema()
        relation = catalog.get("business", "phase10_orders")
        if relation is None:
            raise RuntimeError("synthetic business table was not visible in MCP schema")
        guarded = SQLGuard(catalog).validate(
            'SELECT "id", "amount" FROM "business"."phase10_orders" ORDER BY "id"',
            (),
            page_size=5,
            page_index=0,
        )
        result = await client.execute_query(guarded, catalog, 1)
        return {
            "endpoint": endpoint,
            "source_identity": {
                "host": source["host"],
                "port": source["port"],
                "database_name": source["database_name"],
                "user": "em_p10_business_readonly",
            },
            "schema_relation_count": len(catalog.relations),
            "business_table_visible": True,
            "query_row_count": result.row_count,
            "query_has_more": result.has_more,
            "query_referenced_relation_count": len(result.referenced_relations),
            "readonly_mcp_operation": True,
        }
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:28001/mcp")
    parser.add_argument(
        "--token-file", type=Path, default=ROOT / ".local" / "test-secrets" / "phase10_mcp_token"
    )
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run(args.endpoint, args.token_file))
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
