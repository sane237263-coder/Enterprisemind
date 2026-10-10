import json

import httpx
import pytest

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.database.config import AUTH_HEADER, PROTOCOL_VERSION
from enterprise_mind.database.contracts import SchemaCatalog
from enterprise_mind.database.mcp_server import MAX_REQUEST_BYTES, create_mcp_app


class MetadataService:
    def check_source(self, _value):
        pass

    async def fetch_schema(self):
        return SchemaCatalog(())

    async def close(self):
        pass


class FailingMetadataService(MetadataService):
    def __init__(self, code: ErrorCode):
        self.code = code

    async def fetch_schema(self):
        raise APIError(self.code)


def request():
    return {
        "jsonrpc": "2.0",
        "id": "unit",
        "method": "tools/call",
        "params": {
            "name": "fetch_schema",
            "arguments": {"request": {"source": {}}},
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }


def headers():
    return {
        AUTH_HEADER: "test-token" * 4,
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "MCP-Method": "tools/call",
        "MCP-Name": "fetch_schema",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }


@pytest.mark.parametrize(
    "changed,status",
    [
        ({AUTH_HEADER: "wrong"}, 401),
        ({"Origin": "https://unauthorized.invalid"}, 403),
        ({"Host": "unauthorized.invalid"}, 403),
        ({"MCP-Protocol-Version": "2025-03-26"}, 400),
        ({"Content-Type": "text/plain"}, 415),
    ],
)
async def test_mcp_checks_before_sdk(changed, status):
    app = create_mcp_app(MetadataService(), "test-token" * 4)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        result = await client.post("/mcp", json=request(), headers={**headers(), **changed})
    assert result.status_code == status
    assert "source" not in result.text


async def test_mcp_modern_roundtrip_and_bounded_chunked_body(caplog):
    app = create_mcp_app(MetadataService(), "test-token" * 4)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            result = await client.post("/mcp", json=request(), headers=headers())
            assert result.status_code == 200
            assert result.json()["result"]["structuredContent"]["catalog"] == {"relations": []}
            legacy = request()
            legacy["method"] = "initialize"
            assert (await client.post("/mcp", json=legacy, headers=headers())).status_code == 400

            async def oversized():
                yield b'{"private":"'
                yield b"s" * MAX_REQUEST_BYTES
                yield b'"}'

            result = await client.post("/mcp", content=oversized(), headers=headers())
            assert result.status_code == 413
            assert "private" not in json.dumps(result.json()) + caplog.text


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.DATABASE_CONNECTION_AUTH_FAILED,
        ErrorCode.DATABASE_CONNECTION_NETWORK_FAILED,
        ErrorCode.DATABASE_CONNECTION_PERMISSION_FAILED,
    ],
)
async def test_mcp_connection_test_preserves_safe_failure_category(code):
    app = create_mcp_app(FailingMetadataService(code), "test-token" * 4)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            result = await client.post("/mcp", json=request(), headers=headers())
    assert result.status_code == 200
    assert result.json()["result"]["structuredContent"]["error_code"] == code.value
