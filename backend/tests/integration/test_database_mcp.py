"""Real PostgreSQL and loopback MCP HTTP coverage for the stage-8 boundary.

The fixture owns one random schema and one random login role in the dedicated
business PostgreSQL container.  It never connects to the EnterpriseMind
application database and removes exactly the objects it creates.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import secrets
import socket
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import Implementation
from psycopg import sql

from enterprise_mind.database.catalog import catalog_to_wire
from enterprise_mind.database.config import DatabaseConnectionConfig
from enterprise_mind.database.contracts import SchemaCatalog
from enterprise_mind.database.guard import SQLGuard
from enterprise_mind.database.mcp_client import DatabaseMCPClient
from enterprise_mind.database.mcp_server import DatabaseMCPService, create_mcp_app

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class BusinessFixture:
    admin_conninfo: str
    database_name: str
    port: int
    schema_name: str
    table_name: str
    role_name: str
    role_password_file: Path
    token_file: Path
    token: str

    @property
    def relation_name(self) -> str:
        return f"{self.schema_name}.{self.table_name}"

    @property
    def source_metadata(self) -> dict[str, Any]:
        return {
            "host": "127.0.0.1",
            "port": self.port,
            "database_name": self.database_name,
            "ssl_mode": "disable",
            "secret_ref": "secret:enterprise_database_readonly",
            "connection_options": None,
        }

    def admin(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self.admin_conninfo, autocommit=True)

    def readonly(self) -> psycopg.Connection[Any]:
        password = self.role_password_file.read_text(encoding="utf-8").strip()
        conninfo = (
            "host=127.0.0.1 port={} dbname={} user={} password={} connect_timeout=5"
        ).format(
            self.port,
            self.database_name,
            self.role_name,
            password.replace("\\", "\\\\").replace(" ", "\\ "),
        )
        return psycopg.connect(conninfo, autocommit=True)


@pytest.fixture(scope="module")
def business_fixture(tmp_path_factory: pytest.TempPathFactory) -> BusinessFixture:
    password_path = os.getenv("EM_TEST_BUSINESS_PASSWORD_FILE")
    if not password_path:
        pytest.skip(
            "Set EM_TEST_BUSINESS_PASSWORD_FILE for the isolated business PostgreSQL fixture"
        )
    maintenance_user = os.getenv("EM_TEST_BUSINESS_MAINTENANCE_USER", "em_business_owner")
    maintenance_password_path = Path(
        os.getenv("EM_TEST_BUSINESS_MAINTENANCE_PASSWORD_FILE", password_path)
    )
    maintenance_password = maintenance_password_path.read_text(encoding="utf-8").strip()
    port = int(os.getenv("EM_TEST_BUSINESS_PORT", "55483"))
    maintenance_database = "em_business_test"
    database_name = "em_mcp_it_" + uuid.uuid4().hex[:12]
    maintenance_conninfo = (
        "host=127.0.0.1 port={} dbname={} user={} password={} connect_timeout=5"
    ).format(
        port,
        maintenance_database,
        maintenance_user,
        maintenance_password.replace("\\", "\\\\").replace(" ", "\\ "),
    )
    admin_conninfo = maintenance_conninfo.replace(
        f"dbname={maintenance_database}", f"dbname={database_name}"
    )
    schema_name = "mcp_it_" + uuid.uuid4().hex[:12]
    table_name = "records"
    role_name = "mcp_ro_" + uuid.uuid4().hex[:12]
    role_password = secrets.token_urlsafe(32)
    private_dir = tmp_path_factory.mktemp("database_mcp")
    role_password_file = private_dir / "readonly_password"
    role_password_file.write_text(role_password, encoding="utf-8")
    token = secrets.token_urlsafe(32)
    token_file = private_dir / "mcp_token"
    token_file.write_text(token, encoding="utf-8")

    database_created = False
    try:
        with psycopg.connect(maintenance_conninfo, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER em_business_owner").format(
                    sql.Identifier(database_name)
                )
            )
        database_created = True
        with psycopg.connect(admin_conninfo, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION CURRENT_USER").format(
                    sql.Identifier(schema_name)
                )
            )
            connection.execute(
                sql.SQL(
                    "CREATE TABLE {}.{} ("
                    "id bigint PRIMARY KEY, day_value date NOT NULL, "
                    "amount numeric(18,2) NOT NULL, payload text NOT NULL)"
                ).format(sql.Identifier(schema_name), sql.Identifier(table_name))
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    sql.SQL(
                        "INSERT INTO {}.{} (id, day_value, amount, payload) VALUES (%s, %s, %s, %s)"
                    ).format(sql.Identifier(schema_name), sql.Identifier(table_name)),
                    [
                        (
                            index,
                            f"2026-01-{(index % 28) + 1:02d}",
                            f"{index * 1.25:.2f}",
                            "x" * 20_000,
                        )
                        for index in range(150)
                    ],
                )
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT"
                ).format(sql.Identifier(role_name), sql.Literal(role_password))
            )
            # The reused local PostgreSQL server cannot be changed globally
            # without affecting the application fixtures. Apply the same
            # no-sensitive-statement-logging contract to this synthetic role.
            for setting, value in {
                "log_statement": "none",
                "log_min_error_statement": "panic",
                "log_min_messages": "panic",
                "log_parameter_max_length_on_error": "0",
                "log_min_duration_statement": "-1",
                "log_min_duration_sample": "-1",
                "log_transaction_sample_rate": "0",
            }.items():
                connection.execute(
                    sql.SQL("ALTER ROLE {} SET {} = {}").format(
                        sql.Identifier(role_name),
                        sql.Identifier(setting),
                        sql.Literal(value),
                    )
                )
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database_name), sql.Identifier(role_name)
                )
            )
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema_name), sql.Identifier(role_name)
                )
            )
            connection.execute(
                sql.SQL("GRANT SELECT ON {}.{} TO {}").format(
                    sql.Identifier(schema_name),
                    sql.Identifier(table_name),
                    sql.Identifier(role_name),
                )
            )
            # Public TEMP/CREATE privileges would make the dedicated role
            # fail the MCP role preflight even though it has no table writes.
            connection.execute(
                sql.SQL("REVOKE TEMP ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(database_name)
                )
            )
            connection.execute(
                sql.SQL("REVOKE CREATE ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(database_name)
                )
            )
            connection.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        yield BusinessFixture(
            admin_conninfo,
            database_name,
            port,
            schema_name,
            table_name,
            role_name,
            role_password_file,
            token_file,
            token,
        )
    finally:
        if database_created:
            try:
                with psycopg.connect(maintenance_conninfo, autocommit=True) as connection:
                    connection.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            sql.Identifier(database_name)
                        )
                    )
                    connection.execute(
                        sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name))
                    )
            except psycopg.Error:
                raise RuntimeError("MCP test fixture cleanup failed") from None


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _error_code(error: BaseException) -> str | None:
    value = getattr(error, "code", None)
    return str(getattr(value, "value", value)) if value is not None else None


def _make_catalog(snapshot: SchemaCatalog, relation_name: str) -> SchemaCatalog:
    return SchemaCatalog(tuple(r for r in snapshot.relations if r.qualified_name == relation_name))


def _make_config(fixture: BusinessFixture) -> DatabaseConnectionConfig:
    return DatabaseConnectionConfig(
        host="127.0.0.1",
        port=fixture.port,
        database_name=fixture.database_name,
        user=fixture.role_name,
        password_file=fixture.role_password_file,
        sslmode="disable",
    )


@pytest_asyncio.fixture
async def mcp_endpoint(business_fixture: BusinessFixture):
    port = _free_port()
    endpoint = f"http://127.0.0.1:{port}/mcp"
    config = _make_config(business_fixture)
    service = DatabaseMCPService(config)
    app = create_mcp_app(
        service,
        token=business_fixture.token,
        allowed_origins=(),
        allowed_hosts=(f"127.0.0.1:{port}", "127.0.0.1", "localhost"),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
        )
    )
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "MCP HTTP server did not start"
    try:
        yield endpoint, config, service, business_fixture
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        close = getattr(service, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


@pytest.mark.asyncio
async def test_real_mcp_http_exposes_only_schema_and_readonly_query(mcp_endpoint):
    endpoint, _, _, fixture = mcp_endpoint
    async with httpx.AsyncClient(
        headers={
            "X-EnterpriseMind-MCP-Secret": fixture.token,
            "Authorization": f"Bearer {fixture.token}",
            "MCP-Protocol-Version": "2026-07-28",
        },
        timeout=10,
        trust_env=False,
    ) as http_client:
        async with Client(
            streamable_http_client(endpoint, http_client=http_client),
            mode="2026-07-28",
            cache=None,
            client_info=Implementation(name="EnterpriseMind tests", version="0.1"),
        ) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {"fetch_schema", "execute_query"}


@pytest.mark.asyncio
async def test_real_mcp_client_fetches_schema_and_pages_complete_rows(mcp_endpoint):
    endpoint, _, _, fixture = mcp_endpoint
    client = DatabaseMCPClient(
        base_url=endpoint, token=fixture.token, source=fixture.source_metadata
    )
    snapshot = await client.fetch_schema()
    assert fixture.relation_name in {
        f"{item.schema_name}.{item.relation_name}" for item in snapshot.relations
    }
    catalog = _make_catalog(snapshot, fixture.relation_name)
    sql_text = (
        f'SELECT id, day_value, amount FROM "{fixture.schema_name}"."{fixture.table_name}" '
        "WHERE day_value >= $1::date AND amount >= $2::numeric AND id >= $3::bigint ORDER BY id"
    )
    guarded = SQLGuard(catalog).validate(
        sql_text,
        ("2026-01-01", "0.00", "0"),
        page_size=100,
        page_index=0,
    )
    result = await client.execute_query(guarded, catalog, 1)
    assert result.row_count == 100 and result.has_more
    assert result.rows[0]["id"] == "0"
    assert result.rows[-1]["id"] == "99"
    assert isinstance(result.rows[0]["amount"], str)
    assert result.referenced_relations == (catalog.relations[0].quoted_qualified_name,)
    second = SQLGuard(catalog).validate(
        sql_text, ("2026-01-01", "0.00", "0"), page_size=100, page_index=1
    )
    next_result = await client.execute_query(second, catalog, 1)
    assert next_result.row_count == 50 and not next_result.has_more
    assert next_result.rows[0]["id"] == "100"
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "parameters", "expected_ids"),
    [
        ("", (5,), ["5", "6"]),
        (" LIMIT 1", (5,), ["5"]),
        (" LIMIT $2", (5, 20), ["5", "6"]),
        (" LIMIT $1", (5,), ["5", "6"]),
    ],
)
async def test_real_mcp_first_n_export_preserves_original_predicate_and_order(
    mcp_endpoint, suffix, parameters, expected_ids
):
    endpoint, _, _, fixture = mcp_endpoint
    client = DatabaseMCPClient(
        base_url=endpoint, token=fixture.token, source=fixture.source_metadata
    )
    try:
        snapshot = await client.fetch_schema()
        catalog = _make_catalog(snapshot, fixture.relation_name)
        query = SQLGuard(catalog).validate(
            f'SELECT id, amount FROM "{fixture.schema_name}"."{fixture.table_name}" '
            f"WHERE id >= $1 ORDER BY id{suffix}",
            parameters,
        )
        frames = [
            frame
            async for frame in client.stream_export(
                replace(query, export_limit=2), catalog, 1, batch_size=1
            )
        ]
        ids = [row[0] for frame in frames if frame["type"] == "batch" for row in frame["rows"]]
        assert ids == expected_ids
        assert frames[-1]["complete"] is True
        assert frames[-1]["exported_rows"] == len(expected_ids)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_reparses_original_and_rechecks_readonly_role(mcp_endpoint, caplog):
    endpoint, _, service, fixture = mcp_endpoint
    client = DatabaseMCPClient(endpoint, fixture.token, source=fixture.source_metadata)
    catalog = _make_catalog(await client.fetch_schema(), fixture.relation_name)
    table = catalog.relations[0].quoted_qualified_name
    valid = SQLGuard(catalog).validate(f"SELECT id FROM {table} ORDER BY id", page_size=1)
    try:
        # The server ignores any caller-supplied pagination wrapper.
        result = await client.execute_query(replace(valid, sql=f"DELETE FROM {table}"), catalog, 1)
        assert result.row_count == 1
        with pytest.raises(Exception) as error:
            await client.execute_query(
                replace(valid, validated_query=f"DELETE FROM {table} RETURNING id"), catalog, 1
            )
        assert _error_code(error.value) == "DATABASE_QUERY_REJECTED"
        assert "DELETE FROM" not in str(error.value) + caplog.text
        with fixture.admin() as connection:
            connection.execute(
                sql.SQL("GRANT UPDATE ON {}.{} TO {}").format(
                    sql.Identifier(fixture.schema_name),
                    sql.Identifier(fixture.table_name),
                    sql.Identifier(fixture.role_name),
                )
            )
        try:
            with pytest.raises(Exception) as error:
                await service.fetch_schema()
            assert _error_code(error.value) == "DATABASE_CONNECTION_PERMISSION_FAILED"
        finally:
            with fixture.admin() as connection:
                connection.execute(
                    sql.SQL("REVOKE UPDATE ON {}.{} FROM {}").format(
                        sql.Identifier(fixture.schema_name),
                        sql.Identifier(fixture.table_name),
                        sql.Identifier(fixture.role_name),
                    )
                )
        client.source = {**fixture.source_metadata, "database_name": "wrong-source"}
        with pytest.raises(Exception) as error:
            await client.execute_query(valid, catalog, 1)
        assert _error_code(error.value) == "DATABASE_SOURCE_UNAVAILABLE"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_statement_timeout_and_cancellation_release_connection(mcp_endpoint):
    _, _, service, fixture = mcp_endpoint
    catalog = _make_catalog(await service.fetch_schema(), fixture.relation_name)
    table = catalog.relations[0].quoted_qualified_name
    expensive = f"SELECT count(*) AS total FROM {table} a"
    for alias in ("b", "c", "d", "e"):
        expensive += f" CROSS JOIN {table} {alias}"
    guarded = SQLGuard(catalog).validate(expensive)
    task = asyncio.create_task(service.execute_query(guarded, catalog, 1))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    good = SQLGuard(catalog).validate(f"SELECT id FROM {table} ORDER BY id", page_size=1)
    assert (await service.execute_query(good, catalog, 1)).row_count == 1
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(Exception) as error:
        await service.execute_query(guarded, catalog, 1)
    assert _error_code(error.value) == "DATABASE_QUERY_TIMEOUT"
    assert 14 <= loop.time() - started < 22
    assert (await service.execute_query(good, catalog, 1)).row_count == 1


@pytest.mark.asyncio
async def test_real_mcp_http_cancellation_releases_query_connection(mcp_endpoint):
    endpoint, _, service, fixture = mcp_endpoint
    client = DatabaseMCPClient(
        base_url=endpoint, token=fixture.token, source=fixture.source_metadata
    )
    try:
        catalog = _make_catalog(await client.fetch_schema(), fixture.relation_name)
        table = catalog.relations[0].quoted_qualified_name
        expensive = f"SELECT count(*) AS total FROM {table} a"
        for alias in ("b", "c", "d", "e"):
            expensive += f" CROSS JOIN {table} {alias}"
        guarded = SQLGuard(catalog).validate(expensive)
        started = asyncio.Event()
        finished = asyncio.Event()
        original_execute = service.execute_query
        active_execution: asyncio.Task | None = None

        async def tracked_execute(query, query_catalog, snapshot_id):
            nonlocal active_execution
            active_execution = asyncio.current_task()
            started.set()
            try:
                return await original_execute(query, query_catalog, snapshot_id)
            finally:
                finished.set()

        service.execute_query = tracked_execute
        request_task = asyncio.create_task(client.execute_query(guarded, catalog, 1))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
            await asyncio.wait_for(asyncio.shield(finished.wait()), timeout=5)
        finally:
            if not request_task.done():
                request_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await request_task
            if active_execution is not None and not active_execution.done():
                active_execution.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await active_execution
            service.execute_query = original_execute
        good = SQLGuard(catalog).validate(f"SELECT id FROM {table} ORDER BY id", page_size=1)
        assert (await service.execute_query(good, catalog, 1)).row_count == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_raw_mcp_http_cancellation_releases_query_connection(mcp_endpoint):
    endpoint, _, service, fixture = mcp_endpoint
    catalog = _make_catalog(await service.fetch_schema(), fixture.relation_name)
    table = catalog.relations[0].quoted_qualified_name
    expensive = f"SELECT count(*) AS total FROM {table} a"
    for alias in ("b", "c", "d", "e"):
        expensive += f" CROSS JOIN {table} {alias}"
    guarded = SQLGuard(catalog).validate(expensive)
    started = asyncio.Event()
    finished = asyncio.Event()
    original_execute = service.execute_query
    active_execution: asyncio.Task | None = None

    async def tracked_execute(query, query_catalog, snapshot_id):
        nonlocal active_execution
        active_execution = asyncio.current_task()
        started.set()
        try:
            return await original_execute(query, query_catalog, snapshot_id)
        finally:
            finished.set()

    service.execute_query = tracked_execute
    body = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "tools/call",
        "params": {
            "name": "execute_query",
            "arguments": {
                "request": {
                    "source": fixture.source_metadata,
                    "catalog": catalog_to_wire(catalog),
                    "query": asdict(guarded),
                    "snapshot_id": 1,
                }
            },
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {
                    "name": "EnterpriseMind tests",
                    "version": "0.1",
                },
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    headers = {
        "X-EnterpriseMind-MCP-Secret": fixture.token,
        "Authorization": f"Bearer {fixture.token}",
        "MCP-Protocol-Version": "2026-07-28",
        "MCP-Method": "tools/call",
        "MCP-Name": "execute_query",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    async with httpx.AsyncClient(timeout=None, trust_env=False) as http_client:
        request_task = asyncio.create_task(
            http_client.post(endpoint, headers=headers, content=json.dumps(body))
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
            await asyncio.wait_for(asyncio.shield(finished.wait()), timeout=5)
        finally:
            if not request_task.done():
                request_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await request_task
            if active_execution is not None and not active_execution.done():
                active_execution.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await active_execution
            service.execute_query = original_execute
    good = SQLGuard(catalog).validate(f"SELECT id FROM {table} ORDER BY id", page_size=1)
    assert (await service.execute_query(good, catalog, 1)).row_count == 1


@pytest.mark.asyncio
async def test_real_mcp_rejects_missing_secret_without_reading_body(mcp_endpoint):
    endpoint, _, _, _ = mcp_endpoint
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.post(endpoint, content=b"not a valid MCP body")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_real_mcp_complete_row_budget_and_forbidden_relation(mcp_endpoint):
    endpoint, _, _, fixture = mcp_endpoint
    client = DatabaseMCPClient(
        base_url=endpoint, token=fixture.token, source=fixture.source_metadata
    )
    snapshot = await client.fetch_schema()
    catalog = _make_catalog(snapshot, fixture.relation_name)
    sql_text = f'SELECT id, payload FROM "{fixture.schema_name}"."{fixture.table_name}" ORDER BY id'
    guarded = SQLGuard(catalog).validate(sql_text, (), page_size=100, page_index=0)
    with pytest.raises(Exception) as error:
        await client.execute_query(guarded, catalog, 1)
    assert _error_code(error.value) == "QUERY_RESULT_TOO_LARGE"

    forbidden = SQLGuard(catalog).validate(
        f'SELECT id FROM "{fixture.schema_name}"."{fixture.table_name}"',
        (),
        page_size=1,
        page_index=0,
    )
    forbidden_catalog = SchemaCatalog(())
    with pytest.raises(Exception) as error:
        await client.execute_query(forbidden, forbidden_catalog, 1)
    assert _error_code(error.value) in {
        "DATABASE_RELATION_FORBIDDEN",
        "DATABASE_QUERY_REJECTED",
    }
    assert fixture.table_name not in str(error.value)
    await client.close()


def test_dedicated_mcp_role_cannot_write(business_fixture: BusinessFixture):
    with business_fixture.readonly() as connection:
        with pytest.raises(psycopg.Error):
            connection.execute(
                sql.SQL("CREATE TABLE {}.forbidden (id integer)").format(
                    sql.Identifier(business_fixture.schema_name)
                )
            )
    with business_fixture.readonly() as connection:
        with pytest.raises(psycopg.Error):
            connection.execute(
                sql.SQL("UPDATE {}.{} SET amount = amount + 1").format(
                    sql.Identifier(business_fixture.schema_name),
                    sql.Identifier(business_fixture.table_name),
                )
            )


@pytest.mark.asyncio
async def test_real_mcp_lock_wait_maps_to_query_timeout(mcp_endpoint):
    endpoint, _, _, fixture = mcp_endpoint
    client = DatabaseMCPClient(
        base_url=endpoint, token=fixture.token, source=fixture.source_metadata
    )
    snapshot = await client.fetch_schema()
    catalog = _make_catalog(snapshot, fixture.relation_name)
    sql_text = f'SELECT id FROM "{fixture.schema_name}"."{fixture.table_name}" ORDER BY id'
    guarded = SQLGuard(catalog).validate(sql_text, (), page_size=1, page_index=0)
    with fixture.admin() as lock_connection:
        lock_connection.execute("BEGIN")
        lock_connection.execute(
            sql.SQL("LOCK TABLE {}.{} IN ACCESS EXCLUSIVE MODE").format(
                sql.Identifier(fixture.schema_name), sql.Identifier(fixture.table_name)
            )
        )
        try:
            with pytest.raises(Exception) as error:
                await client.execute_query(guarded, catalog, 1)
            assert _error_code(error.value) == "DATABASE_QUERY_TIMEOUT"
        finally:
            lock_connection.execute("ROLLBACK")
    await client.close()
