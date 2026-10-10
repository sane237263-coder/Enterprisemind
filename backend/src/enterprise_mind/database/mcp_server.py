"""Independent, internal Database MCP process. Never imports the application DB."""

import asyncio
import hmac
import json
import os
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.database.catalog import catalog_from_wire, catalog_to_wire, fetch_catalog
from enterprise_mind.database.config import (
    AUTH_HEADER,
    PROTOCOL_VERSION,
    ConnectionOptions,
    DatabaseConnectionConfig,
    load_server,
)
from enterprise_mind.database.contracts import (
    GeneratedQuery,
    GuardedQuery,
    QueryResult,
    SchemaCatalog,
    relation_signature,
)
from enterprise_mind.database.guard import SQLGuard, SQLGuardError
from enterprise_mind.database.results import (
    ResultColumn,
    ResultTooLargeError,
    is_bytea_type,
    normalize_query_result,
    normalize_value,
)
from enterprise_mind.infrastructure.settings import ConfigurationError, read_secret

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_EXPORT_ROWS = 1_000_000
MAX_EXPORT_BATCH = 1_000
MAX_EXPORT_SERIALIZED_BYTES = 100 * 1024 * 1024
MAX_EXPORT_TIMEOUT_SECONDS = 120
EXPORT_PLAN_TTL_SECONDS = 30 * 60
MAX_EXPORT_PLANS = 256
ROLE_CHECK = """
SELECT NOT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls)
 AND NOT pg_catalog.has_database_privilege(current_database(), 'CREATE')
 AND NOT pg_catalog.has_database_privilege(current_database(), 'TEMP')
 AND NOT EXISTS (
   SELECT 1 FROM pg_catalog.pg_namespace n
   WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
    AND pg_catalog.has_schema_privilege(n.oid, 'CREATE'))
 AND NOT EXISTS (
   SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
   WHERE c.relkind IN ('r','p','v','m','f') AND n.nspname !~ '^pg_'
    AND n.nspname <> 'information_schema'
    AND (c.relowner = r.oid OR pg_catalog.has_table_privilege(c.oid,
         'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')))
 AND NOT EXISTS (
   SELECT 1 FROM pg_catalog.pg_roles other WHERE other.oid <> r.oid
    AND pg_catalog.pg_has_role(r.oid, other.oid, 'MEMBER'))
FROM pg_catalog.pg_roles r WHERE r.rolname=current_user
"""


def _connection_error_code(error: BaseException) -> ErrorCode:
    sqlstate = getattr(error, "sqlstate", None)
    if sqlstate in {"28P01", "28000", "28001", "28P02"}:
        return ErrorCode.DATABASE_CONNECTION_AUTH_FAILED
    if sqlstate == "42501":
        return ErrorCode.DATABASE_CONNECTION_PERMISSION_FAILED
    return ErrorCode.DATABASE_CONNECTION_NETWORK_FAILED


def _bind(value: Any, typename: str) -> Any:
    if value is None:
        return None
    if typename in {"int2", "int4", "int8"}:
        if isinstance(value, bool) or not isinstance(value, int | str):
            raise ValueError
        return int(value)
    if typename == "numeric":
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError
        return number
    if typename == "uuid":
        return UUID(str(value))
    if typename == "date":
        return date.fromisoformat(str(value))
    if typename in {"timestamp", "timestamptz"}:
        return datetime.fromisoformat(str(value))
    if typename in {"time", "timetz"}:
        return time.fromisoformat(str(value))
    if typename in {"json", "jsonb"}:
        return value if isinstance(value, str) else json.dumps(value, allow_nan=False)
    return value


@dataclass(frozen=True, repr=False)
class _ExportPlan:
    """A short-lived export input owned by this MCP process.

    The application task stores only ``handle`` and safe provenance.  SQL,
    bind values, and current-page rows live here until the owner expires or
    releases the plan.  This keeps an independent Worker from needing the
    API process's Python dictionary while preserving the non-persistence
    boundary.
    """

    handle: str
    owner_user_id: int
    conversation_id: int
    scope: str
    source: dict[str, Any]
    catalog: SchemaCatalog
    query: GuardedQuery
    snapshot_id: int
    row_limit: int | None
    page: dict[str, Any] | None
    expires_at: float

    def __repr__(self) -> str:
        return (
            "_ExportPlan(handle=<redacted>, owner_user_id="
            f"{self.owner_user_id!r}, conversation_id={self.conversation_id!r}, "
            f"scope={self.scope!r}, query=<redacted>, page=<redacted>)"
        )


class ExportPlanStore:
    """Bounded process-local owner for cross-process export handles."""

    def __init__(self, *, ttl_seconds: int = EXPORT_PLAN_TTL_SECONDS) -> None:
        if ttl_seconds < 1 or ttl_seconds > EXPORT_PLAN_TTL_SECONDS:
            raise ValueError("invalid export plan TTL")
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, _ExportPlan] = {}
        self._lock = Lock()

    def put(self, plan: _ExportPlan) -> None:
        now = monotonic()
        with self._lock:
            self._purge(now)
            self._items[plan.handle] = plan
            while len(self._items) > MAX_EXPORT_PLANS:
                self._items.pop(next(iter(self._items)))

    def get(self, handle: str, *, owner_user_id: int, conversation_id: int) -> _ExportPlan | None:
        now = monotonic()
        with self._lock:
            self._purge(now)
            plan = self._items.get(handle)
            if (
                plan is None
                or plan.owner_user_id != owner_user_id
                or plan.conversation_id != conversation_id
            ):
                return None
            return plan

    def remove(self, handle: str, *, owner_user_id: int, conversation_id: int) -> None:
        with self._lock:
            plan = self._items.get(handle)
            if plan is not None and (
                plan.owner_user_id == owner_user_id and plan.conversation_id == conversation_id
            ):
                self._items.pop(handle, None)

    def _purge(self, now: float) -> None:
        for handle, plan in list(self._items.items()):
            if plan.expires_at <= now:
                self._items.pop(handle, None)


@asynccontextmanager
async def _export_transaction(conn: Any) -> AsyncIterator[None]:
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            yield
    except asyncpg.InterfaceError:
        # Connection termination can detach the acquired pool proxy immediately.
        # A usable connection's interface errors remain ordinary query rejections.
        try:
            closed = conn.is_closed()
        except asyncpg.InterfaceError:
            closed = True
        if closed:
            raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE) from None
        raise


class DatabaseMCPService:
    def __init__(self, config: DatabaseConnectionConfig) -> None:
        self.config = config
        self._pool: Any = None
        self._pool_lock = asyncio.Lock()
        self.export_plans = ExportPlanStore()

    def check_source(self, value: dict[str, Any]) -> None:
        expected = {
            "host": self.config.host,
            "port": self.config.port,
            "database_name": self.config.database_name,
            "ssl_mode": self.config.sslmode,
            "secret_ref": self.config.secret_ref,
            "connection_options": self.config.connection_options or {},
        }
        if (
            not isinstance(value, dict)
            or {**value, "connection_options": value.get("connection_options") or {}} != expected
        ):
            raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE)

    def register_export_plan(self, request: dict[str, Any]) -> None:
        """Register one opaque plan through the authenticated internal boundary."""

        if set(request) != {
            "handle",
            "owner_user_id",
            "conversation_id",
            "scope",
            "source",
            "catalog",
            "query",
            "snapshot_id",
            "row_limit",
            "page",
            "ttl_seconds",
        }:
            raise ValueError
        handle = request["handle"]
        if (
            not isinstance(handle, str)
            or len(handle) != 32
            or not all(char in "0123456789abcdef" for char in handle)
        ):
            raise ValueError
        owner_user_id = request["owner_user_id"]
        conversation_id = request["conversation_id"]
        snapshot_id = request["snapshot_id"]
        if (
            type(owner_user_id) is not int
            or owner_user_id < 1
            or type(conversation_id) is not int
            or conversation_id < 1
            or type(snapshot_id) is not int
            or snapshot_id < 1
        ):
            raise ValueError
        scope = request["scope"]
        if scope not in {"current_page", "all_matching", "first_n"}:
            raise ValueError
        ttl_seconds = request["ttl_seconds"]
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= self.export_plans.ttl_seconds:
            raise ValueError
        row_limit = request["row_limit"]
        if scope == "first_n":
            if type(row_limit) is not int or not 1 <= row_limit <= 10_000:
                raise ValueError
        elif row_limit is not None:
            raise ValueError
        source = request["source"]
        if not isinstance(source, dict):
            raise ValueError
        self.check_source(source)
        catalog = catalog_from_wire(request["catalog"])
        query = GuardedQuery(**request["query"])
        page = request["page"]
        if scope == "current_page":
            if not isinstance(page, dict) or set(page) != {"columns", "rows", "queried_at"}:
                raise ValueError
            columns = page["columns"]
            rows = page["rows"]
            queried_at = page["queried_at"]
            if (
                not isinstance(columns, list)
                or not columns
                or not isinstance(rows, list)
                or len(rows) > 100
                or not isinstance(queried_at, str)
                or any(not isinstance(column, dict) for column in columns)
                or any(not isinstance(row, dict) for row in rows)
            ):
                raise ValueError
            names = [column.get("name") for column in columns]
            if any(not isinstance(name, str) or not name for name in names) or len(names) != len(
                set(names)
            ):
                raise ValueError
            if any(set(row) != set(names) for row in rows):
                raise ValueError
            page = {"columns": columns, "rows": rows, "queried_at": queried_at}
        elif page is not None:
            raise ValueError
        self.export_plans.put(
            _ExportPlan(
                handle=handle,
                owner_user_id=owner_user_id,
                conversation_id=conversation_id,
                scope=scope,
                source=source,
                catalog=catalog,
                query=query,
                snapshot_id=snapshot_id,
                row_limit=row_limit,
                page=page,
                expires_at=monotonic() + ttl_seconds,
            )
        )

    def release_export_plan(self, request: dict[str, Any]) -> None:
        if set(request) != {"handle", "owner_user_id", "conversation_id"}:
            raise ValueError
        handle = request["handle"]
        owner_user_id = request["owner_user_id"]
        conversation_id = request["conversation_id"]
        if (
            not isinstance(handle, str)
            or type(owner_user_id) is not int
            or type(conversation_id) is not int
        ):
            raise ValueError
        self.export_plans.remove(
            handle, owner_user_id=owner_user_id, conversation_id=conversation_id
        )

    def export_plan(self, request: dict[str, Any]) -> _ExportPlan:
        if set(request) != {"handle", "owner_user_id", "conversation_id"}:
            raise ValueError
        handle = request["handle"]
        owner_user_id = request["owner_user_id"]
        conversation_id = request["conversation_id"]
        if (
            not isinstance(handle, str)
            or type(owner_user_id) is not int
            or type(conversation_id) is not int
        ):
            raise ValueError
        plan = self.export_plans.get(
            handle, owner_user_id=owner_user_id, conversation_id=conversation_id
        )
        if plan is None:
            raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
        return plan

    async def _connection_pool(self) -> Any:
        async with self._pool_lock:
            if self._pool is None:
                options = ConnectionOptions.model_validate(self.config.connection_options or {})
                self._pool = await asyncpg.create_pool(
                    host=self.config.host,
                    port=self.config.port,
                    database=self.config.database_name,
                    user=self.config.user,
                    password=read_secret(self.config.password_file).get_secret_value(),
                    ssl=self.config.sslmode,
                    min_size=0,
                    max_size=5,
                    timeout=options.connect_timeout,
                    target_session_attrs=options.target_session_attrs,
                    command_timeout=20,
                    statement_cache_size=0,
                    server_settings={
                        "application_name": options.application_name,
                        "default_transaction_read_only": "on",
                        "statement_timeout": "15000",
                        "lock_timeout": "1000",
                        "transaction_timeout": "20000",
                        "search_path": "pg_catalog",
                    },
                )
        return self._pool

    async def _check(self, conn: Any) -> None:
        if not await conn.fetchval(ROLE_CHECK):
            raise APIError(ErrorCode.DATABASE_CONNECTION_PERMISSION_FAILED)
        # Reject deployments configured to write statement/parameter errors to server logs.
        rows = await conn.fetch(
            "SELECT name, setting FROM pg_catalog.pg_settings WHERE name = ANY($1::text[])",
            [
                "log_statement",
                "log_min_error_statement",
                "log_min_messages",
                "log_parameter_max_length_on_error",
                "log_min_duration_statement",
                "log_min_duration_sample",
                "log_transaction_sample_rate",
                "pg_stat_statements.track",
                "auto_explain.log_min_duration",
                "pgaudit.log",
            ],
        )
        settings = {row["name"]: row["setting"] for row in rows}
        if (
            settings.get("log_statement") != "none"
            or settings.get("log_min_error_statement") != "panic"
            or settings.get("log_min_messages") != "panic"
            or settings.get("log_parameter_max_length_on_error") != "0"
            or settings.get("log_min_duration_statement") != "-1"
            or settings.get("log_min_duration_sample") != "-1"
            or float(settings.get("log_transaction_sample_rate", "1")) != 0
            or settings.get("pg_stat_statements.track", "none") != "none"
            or settings.get("auto_explain.log_min_duration", "-1") != "-1"
            or settings.get("pgaudit.log", "none") not in {"none", ""}
        ):
            raise APIError(ErrorCode.DATABASE_CONNECTION_PERMISSION_FAILED)

    async def fetch_schema(self) -> SchemaCatalog:
        try:
            async with asyncio.timeout(20):
                pool = await self._connection_pool()
                async with pool.acquire(timeout=5) as conn:
                    async with conn.transaction(isolation="repeatable_read", readonly=True):
                        await self._check(conn)
                        return await fetch_catalog(conn)
        except APIError:
            raise
        except ConfigurationError:
            # A missing/invalid mounted Secret is an authentication/config
            # failure, not an opaque schema failure.  The message remains
            # generic and never contains the Secret path or value.
            raise APIError(ErrorCode.DATABASE_CONNECTION_AUTH_FAILED) from None
        except asyncpg.PostgresError as error:
            raise APIError(_connection_error_code(error)) from None
        except (TimeoutError, OSError, ssl.SSLError, asyncpg.InterfaceError):
            raise APIError(ErrorCode.DATABASE_CONNECTION_NETWORK_FAILED) from None
        except Exception:
            raise APIError(ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE) from None

    async def execute_query(
        self, query: GuardedQuery, catalog: SchemaCatalog, snapshot_id: int
    ) -> QueryResult:
        started = monotonic()
        try:
            if type(snapshot_id) is not int or snapshot_id < 1:
                raise ValueError
            # Ignore the client's wrapped SQL, parameters and claimed relation list.
            # Parse the original SELECT and build the server's own LIMIT/OFFSET again.
            checked = SQLGuard(catalog).validate(
                GeneratedQuery(query.validated_query, query.validated_parameters),
                page_size=query.page_size,
                page_index=query.page_index,
            )
            if not checked.referenced_relations:
                raise ValueError
            if checked.referenced_relations != query.referenced_relations:
                raise ValueError
            async with asyncio.timeout(20):
                pool = await self._connection_pool()
                async with pool.acquire(timeout=5) as conn:
                    async with conn.transaction(isolation="repeatable_read", readonly=True):
                        await self._check(conn)
                        # Metadata drift must not turn a built-in column into a custom type.
                        live = await fetch_catalog(conn)
                        for relation in catalog.relations:
                            if relation.quoted_qualified_name not in checked.referenced_relations:
                                continue
                            current = live.get(*relation.key)
                            if current is None or current.relation_type != relation.relation_type:
                                raise APIError(ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE)
                            # Object identity is the authorization continuity
                            # boundary.  A same-name drop/recreate or a View
                            # definition change must not inherit the old
                            # catalog just because its visible columns match.
                            if (
                                relation.relation_oid is None
                                or relation.relation_signature is None
                                or (
                                    relation.relation_type == "view"
                                    and relation.view_definition_signature is None
                                )
                            ):
                                raise APIError(ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE)
                            if current.relation_oid != relation.relation_oid:
                                raise APIError(ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE)
                            if (
                                current.view_definition_signature
                                != relation.view_definition_signature
                            ):
                                raise APIError(ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE)
                            if relation_signature(current) != relation.relation_signature:
                                raise APIError(ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE)
                            live_types = {c.name: c.data_type for c in current.columns}
                            if any(live_types.get(c.name) != c.data_type for c in relation.columns):
                                raise APIError(ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE)
                            live_nullable = {c.name: c.is_nullable for c in current.columns}
                            if any(
                                c.is_nullable is not None
                                and live_nullable.get(c.name) != c.is_nullable
                                for c in relation.columns
                            ):
                                raise APIError(ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE)
                            if checked.page_index > 0:
                                keys = {
                                    c.column_names
                                    for c in relation.constraints
                                    if c.constraint_type in {"primary_key", "unique"}
                                }
                                live_keys = {
                                    c.column_names
                                    for c in current.constraints
                                    if c.constraint_type in {"primary_key", "unique"}
                                }
                                if keys != live_keys:
                                    raise APIError(ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE)
                        prepared = await conn.prepare(checked.sql)
                        parameters = tuple(
                            _bind(value, kind.name)
                            for value, kind in zip(
                                checked.parameters, prepared.get_parameters(), strict=True
                            )
                        )
                        columns = tuple(
                            ResultColumn(attribute.name, attribute.type.name)
                            for attribute in prepared.get_attributes()
                        )
                        if len({c.name for c in columns}) != len(columns):
                            raise ValueError
                        rows = []
                        async for row in prepared.cursor(*parameters, prefetch=1):
                            rows.append(dict(row))
                            # Normalize incrementally so oversized rows never accumulate.
                            preview = normalize_query_result(
                                columns,
                                rows,
                                page_size=checked.page_size,
                                referenced_relations=checked.referenced_relations,
                                elapsed_ms=(monotonic() - started) * 1000,
                            )
                            if preview.has_more:
                                return preview
                        return normalize_query_result(
                            columns,
                            rows,
                            page_size=checked.page_size,
                            referenced_relations=checked.referenced_relations,
                            elapsed_ms=(monotonic() - started) * 1000,
                        )
        except APIError:
            raise
        except SQLGuardError as error:
            code = (
                ErrorCode.DATABASE_RELATION_FORBIDDEN
                if (error.code == "DATABASE_RELATION_FORBIDDEN")
                else ErrorCode.DATABASE_QUERY_REJECTED
            )
            raise APIError(code) from None
        except (TimeoutError, asyncpg.QueryCanceledError, asyncpg.LockNotAvailableError):
            raise APIError(ErrorCode.DATABASE_QUERY_TIMEOUT) from None
        except ResultTooLargeError:
            raise APIError(ErrorCode.QUERY_RESULT_TOO_LARGE) from None
        except (OSError, asyncpg.PostgresConnectionError):
            raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE) from None
        except Exception:
            # SQL, parameters, driver detail and data values must not cross this boundary.
            raise APIError(ErrorCode.DATABASE_QUERY_REJECTED) from None

    async def stream_export(
        self,
        query: GuardedQuery,
        catalog: SchemaCatalog,
        snapshot_id: int,
        *,
        max_rows: int = MAX_EXPORT_ROWS,
        batch_size: int = MAX_EXPORT_BATCH,
        max_serialized_bytes: int = MAX_EXPORT_SERIALIZED_BYTES,
        timeout_seconds: int = MAX_EXPORT_TIMEOUT_SECONDS,
    ) -> AsyncIterator[dict[str, Any]]:
        """Read one authorized semantic SELECT in one repeatable-read cursor.

        The iterator yields bounded frames and never builds a result list.  The
        SQL guard is reused, but the server removes only its own pagination
        wrapper by executing ``validated_query``; business LIMIT/OFFSET remains
        part of the guarded semantic statement.
        """

        if not 1 <= max_rows <= MAX_EXPORT_ROWS or not 1 <= batch_size <= MAX_EXPORT_BATCH:
            raise APIError(ErrorCode.VALIDATION_ERROR)
        if not 1 <= max_serialized_bytes <= MAX_EXPORT_SERIALIZED_BYTES:
            raise APIError(ErrorCode.VALIDATION_ERROR)
        if not 1 <= timeout_seconds <= MAX_EXPORT_TIMEOUT_SECONDS:
            raise APIError(ErrorCode.VALIDATION_ERROR)
        if query.export_limit is not None and query.export_limit > max_rows:
            raise APIError(ErrorCode.QUERY_RESULT_TOO_LARGE)
        try:
            if type(snapshot_id) is not int or snapshot_id < 1:
                raise ValueError
            checked = SQLGuard(catalog).validate(
                GeneratedQuery(query.validated_query, query.validated_parameters),
                page_size=100,
                page_index=0,
            )
            if not checked.referenced_relations or (
                checked.referenced_relations != query.referenced_relations
            ):
                raise ValueError
            if query.export_limit is not None:
                checked = SQLGuard(catalog).limit_for_export(checked, query.export_limit)
            async with asyncio.timeout(timeout_seconds):
                pool = await self._connection_pool()
                async with pool.acquire(timeout=5) as conn:
                    async with _export_transaction(conn):
                        await self._check(conn)
                        live = await fetch_catalog(conn)
                        for relation in catalog.relations:
                            if relation.quoted_qualified_name not in checked.referenced_relations:
                                continue
                            current = live.get(*relation.key)
                            if current is None or current.relation_type != relation.relation_type:
                                raise APIError(ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE)
                            if (
                                relation.relation_oid is None
                                or relation.relation_signature is None
                                or (
                                    relation.relation_type == "view"
                                    and relation.view_definition_signature is None
                                )
                                or current.relation_oid != relation.relation_oid
                                or current.view_definition_signature
                                != relation.view_definition_signature
                                or relation_signature(current) != relation.relation_signature
                            ):
                                raise APIError(ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE)
                            live_types = {
                                column.name: column.data_type for column in current.columns
                            }
                            if any(
                                live_types.get(column.name) != column.data_type
                                for column in relation.columns
                            ):
                                raise APIError(ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE)
                        prepared = await conn.prepare(checked.validated_query)
                        parameters = tuple(
                            _bind(value, kind.name)
                            for value, kind in zip(
                                checked.validated_parameters,
                                prepared.get_parameters(),
                                strict=True,
                            )
                        )
                        all_columns = tuple(
                            ResultColumn(attribute.name, attribute.type.name)
                            for attribute in prepared.get_attributes()
                        )
                        visible_indexes = tuple(
                            index
                            for index, column in enumerate(all_columns)
                            if not is_bytea_type(column.data_type)
                        )
                        columns = tuple(
                            column for column in all_columns if not is_bytea_type(column.data_type)
                        )
                        if len({column.name for column in columns}) != len(columns):
                            raise ValueError
                        header = {
                            "type": "header",
                            "columns": [asdict(column) for column in columns],
                            "referenced_relations": list(checked.referenced_relations),
                        }
                        total_bytes = len(
                            json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode(
                                "utf-8"
                            )
                        )
                        if total_bytes > max_serialized_bytes:
                            raise ResultTooLargeError("export metadata exceeds budget")
                        yield header
                        count = 0
                        batch: list[list[Any]] = []
                        sequence = 0
                        async for row in prepared.cursor(*parameters, prefetch=batch_size):
                            if query.export_limit is not None and count >= query.export_limit:
                                # A first-N request is a semantic LIMIT, not a
                                # truncation switch. If the guarded semantic
                                # query still yields N+1, fail rather than
                                # publishing an incomplete result as complete.
                                raise ResultTooLargeError(
                                    "first-N semantic limit was not preserved"
                                )
                            if count >= max_rows:
                                raise ResultTooLargeError("export row budget exceeded")
                            raw = tuple(row)
                            if len(raw) != len(all_columns):
                                raise ValueError
                            batch.append(
                                [
                                    normalize_value(raw[index], columns[position].data_type)
                                    for position, index in enumerate(visible_indexes)
                                ]
                            )
                            count += 1
                            if len(batch) >= batch_size:
                                sequence += 1
                                frame = {
                                    "type": "batch",
                                    "sequence": sequence,
                                    "rows": batch,
                                }
                                total_bytes += len(
                                    json.dumps(
                                        frame,
                                        ensure_ascii=False,
                                        allow_nan=False,
                                        separators=(",", ":"),
                                    ).encode("utf-8")
                                )
                                if total_bytes > max_serialized_bytes:
                                    raise ResultTooLargeError("export data budget exceeded")
                                yield frame
                                batch = []
                        if batch:
                            sequence += 1
                            frame = {"type": "batch", "sequence": sequence, "rows": batch}
                            total_bytes += len(
                                json.dumps(
                                    frame,
                                    ensure_ascii=False,
                                    allow_nan=False,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            )
                            if total_bytes > max_serialized_bytes:
                                raise ResultTooLargeError("export data budget exceeded")
                            yield frame
                        end = {
                            "type": "end",
                            "exported_rows": count,
                            "complete": True,
                            "executed_at": datetime.now(UTC).isoformat(),
                        }
                        total_bytes += len(
                            json.dumps(end, ensure_ascii=False, separators=(",", ":")).encode(
                                "utf-8"
                            )
                        )
                        if total_bytes > max_serialized_bytes:
                            raise ResultTooLargeError("export metadata exceeds budget")
                        yield end
        except APIError:
            raise
        except SQLGuardError as error:
            code = (
                ErrorCode.DATABASE_RELATION_FORBIDDEN
                if error.code == "DATABASE_RELATION_FORBIDDEN"
                else ErrorCode.DATABASE_QUERY_REJECTED
            )
            raise APIError(code) from None
        except (TimeoutError, asyncpg.QueryCanceledError, asyncpg.LockNotAvailableError):
            raise APIError(ErrorCode.DATABASE_QUERY_TIMEOUT) from None
        except ResultTooLargeError:
            raise APIError(ErrorCode.QUERY_RESULT_TOO_LARGE) from None
        except (OSError, asyncpg.PostgresConnectionError):
            raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE) from None
        except Exception:
            raise APIError(ErrorCode.DATABASE_QUERY_REJECTED) from None

    async def stream_export_plan(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Resolve one owner-bound handle and stream it through the same MCP guard."""

        expected = {
            "handle",
            "owner_user_id",
            "conversation_id",
            "max_rows",
            "batch_size",
            "max_serialized_bytes",
            "timeout_seconds",
        }
        if set(request) != expected:
            raise ValueError
        plan = self.export_plan(
            {
                "handle": request["handle"],
                "owner_user_id": request["owner_user_id"],
                "conversation_id": request["conversation_id"],
            }
        )
        max_rows = request["max_rows"]
        batch_size = request["batch_size"]
        max_serialized_bytes = request["max_serialized_bytes"]
        timeout_seconds = request["timeout_seconds"]
        if (
            type(max_rows) is not int
            or type(batch_size) is not int
            or type(max_serialized_bytes) is not int
            or type(timeout_seconds) is not int
        ):
            raise ValueError
        if not 1 <= max_rows <= MAX_EXPORT_ROWS or not 1 <= batch_size <= MAX_EXPORT_BATCH:
            raise APIError(ErrorCode.VALIDATION_ERROR)
        if not 1 <= max_serialized_bytes <= MAX_EXPORT_SERIALIZED_BYTES:
            raise APIError(ErrorCode.VALIDATION_ERROR)
        if not 1 <= timeout_seconds <= MAX_EXPORT_TIMEOUT_SECONDS:
            raise APIError(ErrorCode.VALIDATION_ERROR)
        self.check_source(plan.source)
        if plan.scope == "current_page":
            page = plan.page
            if page is None:
                raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
            columns = page["columns"]
            rows = page["rows"]
            if not isinstance(columns, list) or not isinstance(rows, list):
                raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
            names = [column.get("name") for column in columns]
            if any(not isinstance(name, str) or not name for name in names):
                raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
            header = {
                "type": "header",
                "columns": columns,
                "referenced_relations": list(plan.query.referenced_relations),
            }
            total_bytes = len(
                json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            if len(rows) > max_rows:
                raise APIError(ErrorCode.QUERY_RESULT_TOO_LARGE)
            yield header
            if rows:
                batch = {
                    "type": "batch",
                    "sequence": 1,
                    "rows": [[row[name] for name in names] for row in rows],
                }
                total_bytes += len(
                    json.dumps(
                        batch, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    ).encode("utf-8")
                )
                if total_bytes > max_serialized_bytes:
                    raise APIError(ErrorCode.QUERY_RESULT_TOO_LARGE)
                yield batch
            end = {
                "type": "end",
                "exported_rows": len(rows),
                "complete": True,
                "executed_at": page["queried_at"],
            }
            total_bytes += len(
                json.dumps(end, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            if total_bytes > max_serialized_bytes:
                raise APIError(ErrorCode.QUERY_RESULT_TOO_LARGE)
            yield end
            return
        query = plan.query
        if plan.scope == "first_n":
            if plan.row_limit is None:
                raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
            query = replace(query, export_limit=plan.row_limit)
        async for frame in self.stream_export(
            query,
            plan.catalog,
            plan.snapshot_id,
            max_rows=max_rows,
            batch_size=batch_size,
            max_serialized_bytes=max_serialized_bytes,
            timeout_seconds=timeout_seconds,
        ):
            yield frame

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


class RequestBoundary:
    """Authenticate and validate before SDK parsing, including chunked request bodies."""

    def __init__(
        self,
        app: ASGIApp,
        token: str,
        allowed_origins: tuple[str, ...],
        allowed_hosts: tuple[str, ...],
        service: DatabaseMCPService,
    ) -> None:
        self.app, self.token, self.service = app, token, service
        self.origins, self.hosts = allowed_origins, allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def reject(status: int) -> None:
            await JSONResponse(
                {"error": "DATABASE_MCP_REQUEST_REJECTED"},
                status_code=status,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        path = scope.get("path")
        export = path == "/export"
        export_plan_register = path == "/export-plan"
        export_plan_stream = path == "/export-plan/stream"
        export_plan_release = path == "/export-plan/release"
        allowed_paths = {
            "/mcp",
            "/export",
            "/export-plan",
            "/export-plan/stream",
            "/export-plan/release",
        }
        if path not in allowed_paths or scope.get("method") != "POST":
            await reject(405)
            return
        if not hmac.compare_digest(
            headers.get(AUTH_HEADER.lower(), "").encode(), self.token.encode()
        ):
            await reject(401)
            return
        if headers.get("host") not in self.hosts or (
            "origin" in headers and headers["origin"] not in self.origins
        ):
            await reject(403)
            return
        if headers.get("mcp-protocol-version") != PROTOCOL_VERSION:
            await reject(400)
            return
        if headers.get("content-type", "").split(";")[0] != "application/json":
            await reject(415)
            return
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > MAX_REQUEST_BYTES:
                        await reject(413)
                        return
                    if not message.get("more_body", False):
                        break
            request = json.loads(body)
            if export:
                if (
                    not isinstance(request, dict)
                    or set(request)
                    != {
                        "source",
                        "catalog",
                        "query",
                        "snapshot_id",
                        "max_rows",
                        "batch_size",
                        "max_serialized_bytes",
                        "timeout_seconds",
                    }
                    or not isinstance(request["source"], dict)
                    or not isinstance(request["catalog"], dict)
                    or not isinstance(request["query"], dict)
                ):
                    raise ValueError
                self.service.check_source(request["source"])
                catalog = catalog_from_wire(request["catalog"])
                query = GuardedQuery(**request["query"])
                stream = self.service.stream_export(
                    query,
                    catalog,
                    request["snapshot_id"],
                    max_rows=request["max_rows"],
                    batch_size=request["batch_size"],
                    max_serialized_bytes=request["max_serialized_bytes"],
                    timeout_seconds=request["timeout_seconds"],
                )

                async def frames() -> AsyncIterator[bytes]:
                    try:
                        async for frame in stream:
                            yield (
                                json.dumps(
                                    frame,
                                    ensure_ascii=False,
                                    allow_nan=False,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                                + b"\n"
                            )
                    except APIError as error:
                        yield (
                            json.dumps(
                                {"type": "error", "error_code": error.code.value},
                                separators=(",", ":"),
                            ).encode("utf-8")
                            + b"\n"
                        )
                    except Exception:
                        yield b'{"type":"error","error_code":"DATABASE_QUERY_REJECTED"}\n'

                await StreamingResponse(
                    frames(),
                    media_type="application/x-ndjson",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                )(scope, receive, send)
                return
            if export_plan_register:
                self.service.register_export_plan(request)
                await JSONResponse(
                    {"ok": True},
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                )(scope, receive, send)
                return
            if export_plan_release:
                self.service.release_export_plan(request)
                await JSONResponse(
                    {"ok": True},
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                )(scope, receive, send)
                return
            if export_plan_stream:
                # Resolve and validate the handle before starting the response;
                # an expired or cross-user handle must be an ordinary 409-style
                # MCP error and must never produce an empty successful file.
                stream = self.service.stream_export_plan(request)

                async def plan_frames() -> AsyncIterator[bytes]:
                    try:
                        async for frame in stream:
                            yield (
                                json.dumps(
                                    frame,
                                    ensure_ascii=False,
                                    allow_nan=False,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                                + b"\n"
                            )
                    except APIError as error:
                        yield (
                            json.dumps(
                                {"type": "error", "error_code": error.code.value},
                                separators=(",", ":"),
                            ).encode("utf-8")
                            + b"\n"
                        )
                    except Exception:
                        yield b'{"type":"error","error_code":"DATABASE_QUERY_REJECTED"}\n'

                await StreamingResponse(
                    plan_frames(),
                    media_type="application/x-ndjson",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                )(scope, receive, send)
                return
            params = request["params"]
            meta = params["_meta"]
            valid = (
                request.get("jsonrpc") == "2.0"
                and type(request.get("id")) in {str, int}
                and len(str(request["id"])) <= 64
                and meta["io.modelcontextprotocol/protocolVersion"] == PROTOCOL_VERSION
                and isinstance(meta["io.modelcontextprotocol/clientCapabilities"], dict)
                and request.get("method") in {"tools/call", "tools/list", "discovery"}
            )
            if request.get("method") == "tools/call":
                arguments = params.get("arguments", {})
                valid = valid and params.get("name") in {"fetch_schema", "execute_query"}
                valid = valid and isinstance(arguments, dict) and set(arguments) == {"request"}
                valid = valid and isinstance(arguments.get("request"), dict)
            if not valid:
                raise ValueError
        except Exception:
            await reject(400)
            return
        delivered = False
        messages: asyncio.Queue[Any] = asyncio.Queue()
        disconnected = asyncio.Event()

        async def monitor_disconnect() -> None:
            try:
                while True:
                    message = await receive()
                    await messages.put(message)
                    if message["type"] == "http.disconnect":
                        disconnected.set()
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                return

        async def replay() -> Any:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await messages.get()

        async def run_app() -> None:
            await self.app(scope, replay, send)

        monitor_task = asyncio.create_task(monitor_disconnect())
        app_task: asyncio.Task[None] = asyncio.create_task(run_app())
        try:
            done, _ = await asyncio.wait(
                {app_task, monitor_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if app_task in done:
                await app_task
            elif disconnected.is_set():
                app_task.cancel()
                with suppress(asyncio.CancelledError):
                    await app_task
            else:
                await app_task
        finally:
            if not app_task.done():
                app_task.cancel()
                with suppress(asyncio.CancelledError):
                    await app_task
            if not monitor_task.done():
                monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor_task


def create_mcp_app(
    service: DatabaseMCPService,
    token: str,
    *,
    allowed_origins: tuple[str, ...] = (),
    allowed_hosts: tuple[str, ...] = ("testserver", "localhost", "127.0.0.1"),
) -> Starlette:
    if len(token) < 32:
        raise ValueError("MCP service token must contain at least 32 characters")
    mcp = MCPServer("EnterpriseMind Database", version="1.0", log_level="CRITICAL")

    @mcp.tool(structured_output=True)
    async def fetch_schema(request: dict[str, Any]) -> dict[str, Any]:
        """Fetch metadata using fixed catalog queries; no business rows are read."""
        try:
            if set(request) != {"source"}:
                raise ValueError
            service.check_source(request["source"])
            return {"ok": True, "catalog": catalog_to_wire(await service.fetch_schema())}
        except APIError as error:
            return {"ok": False, "error_code": error.code.value}
        except Exception:
            return {"ok": False, "error_code": "SCHEMA_SNAPSHOT_UNAVAILABLE"}

    @mcp.tool(structured_output=True)
    async def execute_query(request: dict[str, Any]) -> dict[str, Any]:
        """Execute a server-validated, bounded SELECT in a read-only transaction."""
        try:
            if set(request) != {"source", "catalog", "query", "snapshot_id"}:
                raise ValueError
            service.check_source(request["source"])
            catalog = catalog_from_wire(request["catalog"])
            guarded = GuardedQuery(**request["query"])
            result = await service.execute_query(guarded, catalog, request["snapshot_id"])
            return {"ok": True, "result": asdict(result)}
        except APIError as error:
            return {"ok": False, "error_code": error.code.value}
        except Exception:
            return {"ok": False, "error_code": "DATABASE_QUERY_REJECTED"}

    app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_REQUEST_BYTES,
        transport_security=TransportSecuritySettings(
            allowed_hosts=list(allowed_hosts), allowed_origins=list(allowed_origins)
        ),
    )
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Starlette) -> Any:
        try:
            async with original_lifespan(application):
                yield
        finally:
            await service.close()

    app.router.lifespan_context = lifespan
    app.add_middleware(
        RequestBoundary,
        token=token,
        service=service,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    return app


def main() -> None:
    import uvicorn

    config = load_server(Path(os.environ["EM_DATABASE_MCP_SERVER_CONFIG"]))
    app = create_mcp_app(
        DatabaseMCPService(config.database),
        read_secret(config.auth_secret_file).get_secret_value(),
        allowed_hosts=tuple(config.allowed_hosts),
        allowed_origins=tuple(config.allowed_origins),
    )
    uvicorn.run(app, host=config.listen_host, port=config.listen_port, access_log=False)


if __name__ == "__main__":
    main()
