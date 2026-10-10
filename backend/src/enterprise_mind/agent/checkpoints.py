"""Framework tables are managed only through the pinned saver public API."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import URL

from enterprise_mind.infrastructure.provision import BACKUP, CHECKPOINT, MIGRATOR, OWNER


def agent_event_loop() -> asyncio.AbstractEventLoop:
    # psycopg requires Selector on Windows. Ingestion workers keep their own
    # default Proactor loop because their parser needs subprocess support.
    return asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()


def connection_string(url: URL) -> str:
    return make_conninfo(
        host=url.host,
        port=url.port or 5432,
        dbname=url.database,
        user=url.username,
        password=url.password,
        connect_timeout=5,
        options="-c search_path=langgraph_checkpoint -c statement_timeout=10000",
    )


@asynccontextmanager
async def open_saver(url: URL) -> AsyncIterator[AsyncPostgresSaver]:
    # All graph values are controlled primitives; arbitrary class reconstruction is prohibited.
    os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
    async with AsyncPostgresSaver.from_conn_string(
        connection_string(url),
        serde=JsonPlusSerializer(allowed_msgpack_modules=None, pickle_fallback=False),
    ) as saver:
        yield saver


async def initialize_checkpoints(url: URL) -> None:
    os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
    async with await AsyncConnection.connect(
        connection_string(url),
        autocommit=True,
        row_factory=dict_row,
        prepare_threshold=0,
    ) as connection:
        if url.username == MIGRATOR:
            await connection.execute(f"SET ROLE {OWNER}")
        await connection.execute("SELECT pg_advisory_lock(4617078203315146320)")
        try:
            await connection.execute("CREATE SCHEMA IF NOT EXISTS langgraph_checkpoint")
            await AsyncPostgresSaver(connection).setup()
            await connection.execute("REVOKE ALL ON SCHEMA langgraph_checkpoint FROM PUBLIC")
            await connection.execute(
                "REVOKE ALL ON ALL TABLES IN SCHEMA langgraph_checkpoint FROM PUBLIC"
            )
            for role, privilege in (
                (CHECKPOINT, "SELECT,INSERT,UPDATE,DELETE"),
                (BACKUP, "SELECT"),
            ):
                present = await (
                    await connection.execute(
                        "SELECT 1 FROM pg_roles WHERE rolname=%s",
                        (role,),
                    )
                ).fetchone()
                if present:
                    await connection.execute(
                        f"GRANT USAGE ON SCHEMA langgraph_checkpoint TO {role}"
                    )
                    await connection.execute(
                        f"GRANT {privilege} ON ALL TABLES IN SCHEMA langgraph_checkpoint TO {role}"
                    )
        finally:
            await connection.execute("SELECT pg_advisory_unlock(4617078203315146320)")
