"""Run one controlled runtime preflight and emit a redacted failure summary."""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import suppress
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from enterprise_mind.database.config import load_database
from enterprise_mind.infrastructure.database import check_database, create_engine
from enterprise_mind.infrastructure.settings import APISettings, IngestionSettings, load_application_config
from enterprise_mind.ingestion.queue import TaskQueue
from enterprise_mind.ingestion.storage import ObjectStorage


async def preflight() -> None:
    settings = APISettings()
    config = load_application_config(settings.application_config)
    ingestion = IngestionSettings()
    results: dict[str, object] = {
        "database_config_loaded": load_database() is not None,
        "ingestion_metadata": {
            "minio_endpoint": ingestion.minio_endpoint,
            "minio_bucket": ingestion.minio_bucket,
            "minio_access_key_file": str(ingestion.minio_access_key_file),
            "redis_host": ingestion.redis_host,
            "redis_port": ingestion.redis_port,
            "redis_username": ingestion.redis_username,
            "redis_stream": ingestion.redis_stream,
        },
    }
    engine = create_engine(settings, config)
    queue = TaskQueue(ingestion)
    try:
        try:
            await check_database(engine)
        except BaseException as error:  # noqa: BLE001 - per-component diagnostic
            results["database"] = {"ok": False, "exception_type": type(error).__name__, "message": redact(str(error))}
        else:
            results["database"] = {"ok": True}
        storage = ObjectStorage(ingestion, config.storage)
        try:
            storage.check()
        except BaseException as error:  # noqa: BLE001 - per-component diagnostic
            results["minio"] = {"ok": False, "exception_type": type(error).__name__, "message": redact(str(error))}
        else:
            results["minio"] = {"ok": True}
        try:
            await queue.check()
        except BaseException as error:  # noqa: BLE001 - per-component diagnostic
            results["redis"] = {"ok": False, "exception_type": type(error).__name__, "message": redact(str(error))}
        else:
            results["redis"] = {"ok": True}
        # Only the three component probes carry an ``ok`` verdict.  The
        # metadata entries above are intentionally descriptive dictionaries
        # and must not make an otherwise healthy preflight report failure.
        if not all(
            isinstance(results.get(component), dict)
            and bool(results[component].get("ok"))
            for component in ("database", "minio", "redis")
        ):
            raise RuntimeError(json.dumps(results, ensure_ascii=False, sort_keys=True))
    finally:
        await queue.close()
        await engine.dispose()


def redact(value: str) -> str:
    # Keep the failure class and operational message while excluding local
    # secret paths and any accidental connection-string fragments.
    for secret_name in (
        "postgres_password",
        "redis_password",
        "minio_app_access",
        "minio_app_secret",
        "token_key",
        "phase10_mcp_token",
        "phase10_business_readonly_password",
    ):
        value = value.replace(secret_name, "<secret-file>")
    return value.replace(str(ROOT), "<workspace>")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    result: dict[str, object]
    try:
        asyncio.run(preflight())
    except BaseException as error:  # noqa: BLE001 - diagnostic boundary
        result = {
            "ok": False,
            "exception_type": type(error).__name__,
            "message": redact(str(error)),
            "traceback": [redact(line.rstrip()) for line in traceback.format_exception(error)],
        }
    else:
        result = {"ok": True}
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "traceback"}, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        raise SystemExit(main())
