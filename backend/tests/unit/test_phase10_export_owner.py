import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.database.contracts import GuardedQuery, SchemaCatalog
from enterprise_mind.database.mcp_client import DatabaseMCPClient
from enterprise_mind.database.mcp_server import create_mcp_app

TOKEN = "phase10-owner-token-" + "x" * 32
SOURCE = {
    "host": "phase10-business",
    "port": 5432,
    "database_name": "phase10_business",
    "ssl_mode": "disable",
    "secret_ref": "secret:phase10-business",
    "connection_options": None,
}


def _service():
    from enterprise_mind.database.mcp_server import DatabaseMCPService

    config = SimpleNamespace(
        host=SOURCE["host"],
        port=SOURCE["port"],
        database_name=SOURCE["database_name"],
        sslmode=SOURCE["ssl_mode"],
        secret_ref=SOURCE["secret_ref"],
        connection_options=None,
    )
    return DatabaseMCPService(config)


def _plan() -> SimpleNamespace:
    query = GuardedQuery(
        sql="SELECT id FROM phase10.records",
        parameters=(),
        referenced_relations=(),
        page_size=100,
        page_index=0,
        validated_query="SELECT id FROM phase10.records",
        validated_parameters=(),
    )
    page = SimpleNamespace(
        columns=({"name": "id", "data_type": "int8"},),
        rows=({"id": "000001"}, {"id": "000002"}),
        queried_at=datetime(2026, 9, 21, 8, 0, tzinfo=UTC),
    )
    return SimpleNamespace(
        handle="a" * 32,
        owner_user_id=11,
        conversation_id=22,
        scope="current_page",
        binding=SimpleNamespace(schema_snapshot_id=1),
        catalog=SchemaCatalog(()),
        guarded_query=query,
        page=page,
        row_limit=None,
        expires_at=time.monotonic() + 60,
    )


async def test_export_plan_owner_streams_across_independent_client_boundary() -> None:
    service = _service()
    app = create_mcp_app(service, TOKEN)
    client = DatabaseMCPClient(
        "http://testserver/mcp",
        TOKEN,
        transport=httpx.ASGITransport(app=app),
        source=SOURCE,
    )
    try:
        await client.register_export_plan(_plan())
        frames = [
            frame
            async for frame in client.stream_export_plan(
                "a" * 32,
                owner_user_id=11,
                conversation_id=22,
                max_rows=100,
                batch_size=10,
                max_serialized_bytes=1024 * 1024,
                timeout_seconds=10,
            )
        ]
        assert frames[0]["type"] == "header"
        assert frames[1]["rows"] == [["000001"], ["000002"]]
        assert frames[-1]["exported_rows"] == 2
        await client.release_export_plan("a" * 32, owner_user_id=11, conversation_id=22)
        assert service.export_plans.get("a" * 32, owner_user_id=11, conversation_id=22) is None
    finally:
        await client.close()


async def test_export_plan_owner_binds_handle_to_user_and_conversation() -> None:
    service = _service()
    app = create_mcp_app(service, TOKEN)
    client = DatabaseMCPClient(
        "http://testserver/mcp",
        TOKEN,
        transport=httpx.ASGITransport(app=app),
        source=SOURCE,
    )
    try:
        await client.register_export_plan(_plan())
        with pytest.raises(APIError) as error:
            _ = [
                frame
                async for frame in client.stream_export_plan(
                    "a" * 32,
                    owner_user_id=99,
                    conversation_id=22,
                    max_rows=100,
                    batch_size=10,
                    max_serialized_bytes=1024 * 1024,
                    timeout_seconds=10,
                )
            ]
        assert error.value.code == ErrorCode.QUERY_CONTEXT_EXPIRED
    finally:
        await client.close()


async def test_export_plan_owner_rejects_invalid_stream_budget_before_page_rows() -> None:
    service = _service()
    app = create_mcp_app(service, TOKEN)
    client = DatabaseMCPClient(
        "http://testserver/mcp",
        TOKEN,
        transport=httpx.ASGITransport(app=app),
        source=SOURCE,
    )
    try:
        await client.register_export_plan(_plan())
        stream = service.stream_export_plan(
            {
                "handle": "a" * 32,
                "owner_user_id": 11,
                "conversation_id": 22,
                "max_rows": 0,
                "batch_size": 10,
                "max_serialized_bytes": 1024 * 1024,
                "timeout_seconds": 10,
            }
        )
        with pytest.raises(APIError) as error:
            await anext(stream)
        assert error.value.code == ErrorCode.VALIDATION_ERROR
    finally:
        await client.close()
