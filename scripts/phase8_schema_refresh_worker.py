"""Poll the dedicated schema-refresh Outbox for a disposable Phase-8 session."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from enterprise_mind.agent.checkpoints import agent_event_loop
from enterprise_mind.database.config import load_database
from enterprise_mind.database.mcp_client import create_client
from enterprise_mind.database.service import poll_schema_refresh_outbox
from enterprise_mind.infrastructure.database import create_engine, session_factory
from enterprise_mind.infrastructure.settings import APISettings, load_application_config


async def run(stop_file: Path) -> None:
    settings = APISettings()
    application = load_application_config(settings.application_config)
    database = load_database()
    if database is None:
        raise RuntimeError("Phase-8 schema refresh worker requires Database MCP configuration")
    engine = create_engine(settings, application, role="worker")
    sessions = session_factory(engine)
    client = create_client(database, sessions)
    try:
        while not stop_file.exists():
            async with sessions() as session, session.begin():
                await poll_schema_refresh_outbox(session, client.fetch_schema, limit=10)
            await asyncio.sleep(0.5)
    finally:
        await client.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop-file", type=Path, required=True)
    args = parser.parse_args()
    with asyncio.Runner(loop_factory=agent_event_loop) as runner:
        runner.run(run(args.stop_file))


if __name__ == "__main__":
    main()
