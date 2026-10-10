"""Non-caching MCP 2026-07-28 client, with per-request identity and no sessions."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.database.catalog import catalog_from_wire, catalog_to_wire
from enterprise_mind.database.config import AUTH_HEADER, PROTOCOL_VERSION, DatabaseMCPConfig
from enterprise_mind.database.contracts import GuardedQuery, QueryResult, SchemaCatalog
from enterprise_mind.database.export_runtime import EXPORT_PLAN_TTL_SECONDS
from enterprise_mind.database.models import DatabaseSource, SchemaSnapshot
from enterprise_mind.database.results import RESULT_BUDGET_BYTES, ResultColumn
from enterprise_mind.infrastructure.settings import read_secret

SourceProvider = Callable[[int], Awaitable[dict[str, Any]]]


def source_metadata(source: Any) -> dict[str, Any]:
    if isinstance(source, dict):
        return source
    return {
        "host": source.host,
        "port": source.port,
        "database_name": source.database_name,
        "ssl_mode": source.ssl_mode,
        "secret_ref": source.secret_ref,
        "connection_options": source.connection_options,
    }


class DatabaseMCPClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        source: dict[str, Any] | None = None,
        source_provider: SourceProvider | None = None,
    ) -> None:
        self.source, self.source_provider = source, source_provider
        self._http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            headers={
                AUTH_HEADER: token,
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Accept": "application/json, text/event-stream",
            },
            timeout=httpx.Timeout(25, connect=5),
            follow_redirects=False,
            trust_env=False,
        )
        self._url = base_url

    async def _call(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = uuid4().hex
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": {"request": payload},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientInfo": {
                        "name": "EnterpriseMind",
                        "version": "1.0",
                    },
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }
        try:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > 2 * 1024 * 1024:
                raise ValueError
            async with self._http.stream(
                "POST",
                self._url,
                content=encoded,
                headers={
                    "Content-Type": "application/json",
                    "MCP-Method": "tools/call",
                    "MCP-Name": name,
                },
            ) as response:
                if response.status_code != 200:
                    raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 16 * 1024 * 1024:
                        raise ValueError
            envelope = json.loads(raw)
            if envelope.get("id") != request_id or envelope.get("jsonrpc") != "2.0":
                raise ValueError
            tool_result = envelope["result"]
            if tool_result.get("isError", False):
                raise ValueError
            result = tool_result["structuredContent"]
            if not isinstance(result, dict):
                raise ValueError
            if not result.get("ok"):
                code = result.get("error_code")
                allowed = {
                    item.value: item
                    for item in (
                        ErrorCode.DATABASE_SOURCE_UNAVAILABLE,
                        ErrorCode.DATABASE_CONNECTION_AUTH_FAILED,
                        ErrorCode.DATABASE_CONNECTION_NETWORK_FAILED,
                        ErrorCode.DATABASE_CONNECTION_PERMISSION_FAILED,
                        ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE,
                        ErrorCode.DATABASE_RELATION_FORBIDDEN,
                        ErrorCode.DATABASE_QUERY_REJECTED,
                        ErrorCode.DATABASE_QUERY_TIMEOUT,
                        ErrorCode.QUERY_RESULT_TOO_LARGE,
                    )
                }
                raise APIError(allowed.get(str(code), ErrorCode.DATABASE_QUERY_REJECTED))
            return result
        except APIError:
            raise
        except httpx.TimeoutException:
            raise APIError(ErrorCode.DATABASE_QUERY_TIMEOUT) from None
        except Exception:
            raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE) from None

    async def fetch_schema(self, source: Any = None) -> SchemaCatalog:
        metadata = source_metadata(source) if source is not None else self.source
        response = await self._call("fetch_schema", {"source": metadata})
        try:
            return catalog_from_wire(response["catalog"])
        except Exception:
            raise APIError(ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE) from None

    async def execute_query(
        self, query: GuardedQuery, catalog: SchemaCatalog, snapshot_id: int
    ) -> QueryResult:
        metadata = await self.source_provider(snapshot_id) if self.source_provider else self.source
        response = await self._call(
            "execute_query",
            {
                "source": metadata,
                "catalog": catalog_to_wire(catalog),
                "query": asdict(query),
                "snapshot_id": snapshot_id,
            },
        )
        try:
            result = response["result"]
            if (
                len(
                    json.dumps(
                        result, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                    ).encode("utf-8")
                )
                > RESULT_BUDGET_BYTES
            ):
                raise APIError(ErrorCode.QUERY_RESULT_TOO_LARGE)
            result["columns"] = tuple(ResultColumn(**column) for column in result["columns"])
            parsed = QueryResult(**result)
            if parsed.row_count > query.page_size or (
                parsed.referenced_relations != query.referenced_relations
            ):
                raise ValueError
            return parsed
        except APIError:
            raise
        except Exception:
            raise APIError(ErrorCode.DATABASE_QUERY_REJECTED) from None

    async def register_export_plan(self, plan: Any) -> None:
        """Register one short-lived plan with the independent MCP owner.

        Only the owner process retains SQL, bind values, and current-page rows;
        the durable application task keeps the opaque handle and provenance.
        """

        metadata = (
            await self.source_provider(plan.binding.schema_snapshot_id)
            if self.source_provider
            else self.source
        )
        ttl_seconds = int(plan.expires_at - time.monotonic())
        if ttl_seconds < 1:
            raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
        page = None
        if plan.page is not None:
            page = {
                "columns": [dict(column) for column in plan.page.columns],
                "rows": [dict(row) for row in plan.page.rows],
                "queried_at": plan.page.queried_at.isoformat(),
            }
        body = {
            "handle": plan.handle,
            "owner_user_id": plan.owner_user_id,
            "conversation_id": plan.conversation_id,
            "scope": plan.scope,
            "source": metadata,
            "catalog": catalog_to_wire(plan.catalog),
            "query": asdict(plan.guarded_query),
            "snapshot_id": plan.binding.schema_snapshot_id,
            "row_limit": plan.row_limit,
            "page": page,
            "ttl_seconds": min(EXPORT_PLAN_TTL_SECONDS, ttl_seconds),
        }
        try:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > 2 * 1024 * 1024:
                raise ValueError
            response = await self._http.post(
                self._url.removesuffix("/mcp") + "/export-plan",
                content=encoded,
                headers={"Content-Type": "application/json"},
                timeout=httpx.Timeout(10, connect=5),
            )
            if response.status_code != 200 or response.json().get("ok") is not True:
                raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE)
        except APIError:
            raise
        except httpx.TimeoutException:
            raise APIError(ErrorCode.DATABASE_QUERY_TIMEOUT) from None
        except Exception:
            raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE) from None

    async def release_export_plan(
        self, handle: str, *, owner_user_id: int, conversation_id: int
    ) -> None:
        body = {
            "handle": handle,
            "owner_user_id": owner_user_id,
            "conversation_id": conversation_id,
        }
        try:
            response = await self._http.post(
                self._url.removesuffix("/mcp") + "/export-plan/release",
                content=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                timeout=httpx.Timeout(10, connect=5),
            )
            if response.status_code != 200:
                raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE)
        except APIError:
            raise
        except Exception:
            # Expiry remains the owner-side cleanup if the release transport is gone.
            return

    async def stream_export_plan(
        self,
        handle: str,
        *,
        owner_user_id: int,
        conversation_id: int,
        max_rows: int = 1_000_000,
        batch_size: int = 1_000,
        max_serialized_bytes: int = 100 * 1024 * 1024,
        timeout_seconds: int = 120,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream an owner-resolved plan without sending SQL from the Worker."""

        body = {
            "handle": handle,
            "owner_user_id": owner_user_id,
            "conversation_id": conversation_id,
            "max_rows": max_rows,
            "batch_size": batch_size,
            "max_serialized_bytes": max_serialized_bytes,
            "timeout_seconds": timeout_seconds,
        }
        try:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            async with self._http.stream(
                "POST",
                self._url.removesuffix("/mcp") + "/export-plan/stream",
                content=encoded,
                timeout=httpx.Timeout(timeout_seconds + 5, connect=5),
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status_code != 200:
                    raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE)
                saw_header = False
                saw_end = False
                expected_sequence = 1
                exported_rows = 0
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    frame = json.loads(line)
                    if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
                        raise ValueError
                    if frame["type"] == "error":
                        code = frame.get("error_code")
                        allowed = {
                            item.value: item
                            for item in (
                                ErrorCode.DATABASE_SOURCE_UNAVAILABLE,
                                ErrorCode.DATABASE_CONNECTION_AUTH_FAILED,
                                ErrorCode.DATABASE_CONNECTION_NETWORK_FAILED,
                                ErrorCode.DATABASE_CONNECTION_PERMISSION_FAILED,
                                ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE,
                                ErrorCode.DATABASE_RELATION_FORBIDDEN,
                                ErrorCode.DATABASE_QUERY_REJECTED,
                                ErrorCode.DATABASE_QUERY_TIMEOUT,
                                ErrorCode.QUERY_RESULT_TOO_LARGE,
                                ErrorCode.QUERY_CONTEXT_EXPIRED,
                            )
                        }
                        raise APIError(allowed.get(str(code), ErrorCode.DATABASE_QUERY_REJECTED))
                    if frame["type"] == "header":
                        if saw_header or saw_end:
                            raise ValueError
                        columns = frame.get("columns")
                        relations = frame.get("referenced_relations")
                        if (
                            not isinstance(columns, list)
                            or not columns
                            or not isinstance(relations, list)
                            or any(not isinstance(value, str) for value in relations)
                        ):
                            raise ValueError
                        saw_header = True
                    elif frame["type"] == "batch":
                        if not saw_header or saw_end:
                            raise ValueError
                        if frame.get("sequence") != expected_sequence:
                            raise ValueError
                        rows = frame.get("rows")
                        if not isinstance(rows, list):
                            raise ValueError
                        exported_rows += len(rows)
                        if exported_rows > max_rows:
                            raise APIError(ErrorCode.QUERY_RESULT_TOO_LARGE)
                        expected_sequence += 1
                    elif frame["type"] == "end":
                        if not saw_header or saw_end or frame.get("complete") is not True:
                            raise ValueError
                        if frame.get("exported_rows") != exported_rows:
                            raise ValueError
                        saw_end = True
                    else:
                        raise ValueError
                    yield frame
                if not saw_header or not saw_end:
                    raise ValueError
        except APIError:
            raise
        except httpx.TimeoutException:
            raise APIError(ErrorCode.DATABASE_QUERY_TIMEOUT) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE) from None

    async def stream_export(
        self,
        query: GuardedQuery,
        catalog: SchemaCatalog,
        snapshot_id: int,
        *,
        max_rows: int = 1_000_000,
        batch_size: int = 1_000,
        max_serialized_bytes: int = 100 * 1024 * 1024,
        timeout_seconds: int = 120,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield frames from the independent MCP export cursor.

        Frames are consumed by the XLSX writer; this method intentionally
        never assembles all rows into a response object.
        """

        metadata = await self.source_provider(snapshot_id) if self.source_provider else self.source
        body = {
            "source": metadata,
            "catalog": catalog_to_wire(catalog),
            "query": asdict(query),
            "snapshot_id": snapshot_id,
            "max_rows": max_rows,
            "batch_size": batch_size,
            "max_serialized_bytes": max_serialized_bytes,
            "timeout_seconds": timeout_seconds,
        }
        try:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > 2 * 1024 * 1024:
                raise ValueError
            endpoint = self._url.removesuffix("/mcp") + "/export"
            async with self._http.stream(
                "POST",
                endpoint,
                content=encoded,
                timeout=httpx.Timeout(timeout_seconds + 5, connect=5),
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status_code != 200:
                    raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE)
                saw_header = False
                saw_end = False
                expected_sequence = 1
                exported_rows = 0
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    frame = json.loads(line)
                    if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
                        raise ValueError
                    if frame["type"] == "error":
                        code = frame.get("error_code")
                        allowed = {
                            item.value: item
                            for item in (
                                ErrorCode.DATABASE_SOURCE_UNAVAILABLE,
                                ErrorCode.DATABASE_CONNECTION_AUTH_FAILED,
                                ErrorCode.DATABASE_CONNECTION_NETWORK_FAILED,
                                ErrorCode.DATABASE_CONNECTION_PERMISSION_FAILED,
                                ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE,
                                ErrorCode.DATABASE_RELATION_FORBIDDEN,
                                ErrorCode.DATABASE_QUERY_REJECTED,
                                ErrorCode.DATABASE_QUERY_TIMEOUT,
                                ErrorCode.QUERY_RESULT_TOO_LARGE,
                            )
                        }
                        raise APIError(allowed.get(str(code), ErrorCode.DATABASE_QUERY_REJECTED))
                    if frame["type"] == "header":
                        if saw_header or saw_end:
                            raise ValueError
                        columns = frame.get("columns")
                        if (
                            not isinstance(columns, list)
                            or not columns
                            or frame.get("referenced_relations") != list(query.referenced_relations)
                        ):
                            raise ValueError
                        saw_header = True
                    elif frame["type"] == "batch":
                        if not saw_header or saw_end:
                            raise ValueError
                        if frame.get("sequence") != expected_sequence:
                            raise ValueError
                        rows = frame.get("rows")
                        if not isinstance(rows, list):
                            raise ValueError
                        exported_rows += len(rows)
                        expected_sequence += 1
                    elif frame["type"] == "end":
                        if not saw_header or saw_end or frame.get("complete") is not True:
                            raise ValueError
                        if frame.get("exported_rows") != exported_rows:
                            raise ValueError
                        if query.export_limit is not None and exported_rows > query.export_limit:
                            raise ValueError
                        saw_end = True
                    else:
                        raise ValueError
                    yield frame
                if not saw_header or not saw_end:
                    raise ValueError
        except APIError:
            raise
        except httpx.TimeoutException:
            raise APIError(ErrorCode.DATABASE_QUERY_TIMEOUT) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE) from None

    async def close(self) -> None:
        await self._http.aclose()


def create_client(
    config: DatabaseMCPConfig,
    sessions: async_sessionmaker[AsyncSession],
) -> DatabaseMCPClient:
    async def source_provider(snapshot_id: int) -> dict[str, Any]:
        async with sessions() as session:
            snapshot = await session.get(SchemaSnapshot, snapshot_id)
            source = (
                await session.get(DatabaseSource, snapshot.database_source_id) if snapshot else None
            )
            if source is None or source.status != "active":
                raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE)
            return source_metadata(source)

    return DatabaseMCPClient(
        config.endpoint,
        read_secret(config.auth_secret_file).get_secret_value(),
        source_provider=source_provider,
    )
