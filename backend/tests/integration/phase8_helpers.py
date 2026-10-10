"""Disposable Phase-8 business database and loopback MCP helpers.

The objects created here are synthetic and randomised per test module.  The
helper deliberately keeps connection passwords and the MCP bearer token in
temporary files; callers only receive non-secret source metadata.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from psycopg import sql

from enterprise_mind.agent.checkpoints import agent_event_loop
from enterprise_mind.database.config import DatabaseConnectionConfig
from enterprise_mind.database.mcp_client import DatabaseMCPClient
from enterprise_mind.database.service import poll_schema_refresh_outbox
from enterprise_mind.infrastructure.database import create_engine, session_factory
from enterprise_mind.infrastructure.settings import ApplicationConfig, read_secret


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def wait_for_port(process: subprocess.Popen[bytes], port: int, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Phase-8 helper process exited during startup")
        if port_open(port):
            return
        time.sleep(0.1)
    raise RuntimeError("Phase-8 helper process did not open its port")


def stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


@dataclass(frozen=True)
class Phase8BusinessFixture:
    admin_conninfo: str
    database_name: str
    port: int
    role_name: str
    role_password_file: Path
    token_file: Path
    token: str
    sales_schema: str
    archive_schema: str
    finance_schema: str
    misc_schema: str
    private_schema: str
    orders_table: str = "orders"
    secure_view: str = "secure_orders"
    finance_table: str = "budget"
    readable_table: str = "readable_not_whitelisted"
    private_table: str = "sentinel"

    @property
    def orders_relation(self) -> str:
        return f"{self.sales_schema}.{self.orders_table}"

    @property
    def archive_relation(self) -> str:
        return f"{self.archive_schema}.{self.orders_table}"

    @property
    def secure_relation(self) -> str:
        return f"{self.sales_schema}.{self.secure_view}"

    @property
    def finance_relation(self) -> str:
        return f"{self.finance_schema}.{self.finance_table}"

    @property
    def readable_relation(self) -> str:
        return f"{self.misc_schema}.{self.readable_table}"

    @property
    def source_metadata(self) -> dict[str, Any]:
        return {
            "host": "127.0.0.1",
            "port": self.port,
            "database_name": self.database_name,
            "ssl_mode": "disable",
            "secret_ref": "secret:phase8_business_readonly",
            "connection_options": None,
        }

    @property
    def source_body(self) -> dict[str, Any]:
        return {
            "name": "Phase 8 synthetic business database",
            **self.source_metadata,
        }

    def admin(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self.admin_conninfo, autocommit=True)

    def readonly(self) -> psycopg.Connection[Any]:
        password = read_secret(self.role_password_file).get_secret_value()
        conninfo = (
            "host=127.0.0.1 port={} dbname={} user={} password={} connect_timeout=5"
        ).format(
            self.port,
            self.database_name,
            self.role_name,
            password.replace("\\", "\\\\").replace(" ", "\\ "),
        )
        return psycopg.connect(conninfo, autocommit=True)


@dataclass(frozen=True)
class Phase8MCPFixture:
    business: Phase8BusinessFixture
    endpoint: str
    port: int
    server_config_file: Path


def _maintenance_conninfo(password_path: Path, port: int) -> str:
    user = os.getenv("EM_TEST_BUSINESS_MAINTENANCE_USER", "em_business_owner")
    database = os.getenv("EM_TEST_BUSINESS_DATABASE", "em_business_test")
    password = read_secret(password_path).get_secret_value()
    return ("host=127.0.0.1 port={} dbname={} user={} password={} connect_timeout=5").format(
        port, database, user, password.replace("\\", "\\\\").replace(" ", "\\ ")
    )


def _create_business_fixture(tmp_path_factory: pytest.TempPathFactory) -> Phase8BusinessFixture:
    password_path = os.getenv("EM_TEST_BUSINESS_PASSWORD_FILE")
    if not password_path:
        pytest.skip("Set EM_TEST_BUSINESS_PASSWORD_FILE for the Phase-8 business fixture")
    maintenance_password_path = Path(
        os.getenv("EM_TEST_BUSINESS_MAINTENANCE_PASSWORD_FILE", password_path)
    )
    port = int(os.getenv("EM_TEST_BUSINESS_PORT", "55484"))
    maintenance_conninfo = _maintenance_conninfo(maintenance_password_path, port)
    database_name = "em_phase8_" + uuid.uuid4().hex[:16]
    role_name = "phase8_ro_" + uuid.uuid4().hex[:12]
    role_password = secrets.token_urlsafe(32)
    private_dir = tmp_path_factory.mktemp("phase8-business")
    role_password_file = private_dir / "readonly_password"
    role_password_file.write_text(role_password, encoding="utf-8")
    token = secrets.token_urlsafe(32)
    token_file = private_dir / "mcp_token"
    token_file.write_text(token, encoding="utf-8")
    admin_conninfo = maintenance_conninfo.replace(
        f"dbname={os.getenv('EM_TEST_BUSINESS_DATABASE', 'em_business_test')}",
        f"dbname={database_name}",
    )
    suffix = uuid.uuid4().hex[:10]
    schemas = {
        "sales": f"phase8_sales_{suffix}",
        "archive": f"phase8_archive_{suffix}",
        "finance": f"phase8_finance_{suffix}",
        "misc": f"phase8_misc_{suffix}",
        "private": f"phase8_private_{suffix}",
    }
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
            for schema_name in schemas.values():
                connection.execute(
                    sql.SQL("CREATE SCHEMA {} AUTHORIZATION CURRENT_USER").format(
                        sql.Identifier(schema_name)
                    )
                )
            connection.execute(
                sql.SQL(
                    "CREATE TABLE {}.orders ("
                    "id bigint PRIMARY KEY, order_date date NOT NULL, "
                    "amount numeric(18,2) NOT NULL, status text NOT NULL, "
                    "department text NOT NULL)"
                ).format(sql.Identifier(schemas["sales"]))
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    sql.SQL(
                        "INSERT INTO {}.orders "
                        "(id, order_date, amount, status, department) "
                        "VALUES (%s, %s, %s, %s, %s)"
                    ).format(sql.Identifier(schemas["sales"])),
                    [
                        (index, f"2026-01-{(index % 28) + 1:02d}", "10.00", "paid", "sales")
                        for index in range(1, 236)
                    ],
                )
            connection.execute(
                sql.SQL(
                    "CREATE VIEW {}.secure_orders WITH (security_barrier=true) AS "
                    "SELECT id, order_date, amount, status FROM {}.orders "
                    "WHERE status = 'paid'"
                ).format(sql.Identifier(schemas["sales"]), sql.Identifier(schemas["sales"]))
            )
            connection.execute(
                sql.SQL(
                    "COMMENT ON TABLE {}.orders IS "
                    "'Synthetic paid sales orders with stable primary-key paging'"
                ).format(sql.Identifier(schemas["sales"]))
            )
            connection.execute(
                sql.SQL(
                    "CREATE TABLE {}.orders ("
                    "id bigint PRIMARY KEY, order_date date NOT NULL, "
                    "amount numeric(18,2) NOT NULL, status text NOT NULL)"
                ).format(sql.Identifier(schemas["archive"]))
            )
            connection.execute(
                sql.SQL(
                    "INSERT INTO {}.orders (id, order_date, amount, status) "
                    "VALUES (1, '2025-12-31', '99.00', 'paid')"
                ).format(sql.Identifier(schemas["archive"]))
            )
            connection.execute(
                sql.SQL(
                    "CREATE TABLE {}.budget ("
                    "id bigint PRIMARY KEY, department text NOT NULL, "
                    "amount numeric(18,2) NOT NULL)"
                ).format(sql.Identifier(schemas["finance"]))
            )
            connection.execute(
                sql.SQL(
                    "INSERT INTO {}.budget (id, department, amount) "
                    "VALUES (1, 'finance', '7300.00')"
                ).format(sql.Identifier(schemas["finance"]))
            )
            connection.execute(
                sql.SQL(
                    "CREATE TABLE {}.readable_not_whitelisted ("
                    "id bigint PRIMARY KEY, value text NOT NULL)"
                ).format(sql.Identifier(schemas["misc"]))
            )
            connection.execute(
                sql.SQL(
                    "INSERT INTO {}.readable_not_whitelisted (id, value) "
                    "VALUES (1, 'SYNTHETIC_READABLE_NOT_WHITELISTED')"
                ).format(sql.Identifier(schemas["misc"]))
            )
            connection.execute(
                sql.SQL(
                    "CREATE TABLE {}.sentinel (id bigint PRIMARY KEY, value text NOT NULL)"
                ).format(sql.Identifier(schemas["private"]))
            )
            connection.execute(
                sql.SQL(
                    "INSERT INTO {}.sentinel (id, value) "
                    "VALUES (1, 'PRIVATE_DB_SENTINEL_DO_NOT_DISCLOSE')"
                ).format(sql.Identifier(schemas["private"]))
            )
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT"
                ).format(sql.Identifier(role_name), sql.Literal(role_password))
            )
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
                        sql.Identifier(role_name), sql.Identifier(setting), sql.Literal(value)
                    )
                )
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database_name), sql.Identifier(role_name)
                )
            )
            for schema_name in schemas.values():
                connection.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier(schema_name), sql.Identifier(role_name)
                    )
                )
            for schema_name, relation_name in (
                (schemas["sales"], "orders"),
                (schemas["sales"], "secure_orders"),
                (schemas["archive"], "orders"),
                (schemas["finance"], "budget"),
                (schemas["misc"], "readable_not_whitelisted"),
            ):
                connection.execute(
                    sql.SQL("GRANT SELECT ON {}.{} TO {}").format(
                        sql.Identifier(schema_name),
                        sql.Identifier(relation_name),
                        sql.Identifier(role_name),
                    )
                )
            # ROLE_CHECK rejects database-level CREATE/TEMP and public-schema
            # CREATE even when the role owns no business objects.
            connection.execute(
                sql.SQL("REVOKE TEMP, CREATE ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(database_name)
                )
            )
            connection.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        yield_fixture = Phase8BusinessFixture(
            admin_conninfo=admin_conninfo,
            database_name=database_name,
            port=port,
            role_name=role_name,
            role_password_file=role_password_file,
            token_file=token_file,
            token=token,
            sales_schema=schemas["sales"],
            archive_schema=schemas["archive"],
            finance_schema=schemas["finance"],
            misc_schema=schemas["misc"],
            private_schema=schemas["private"],
        )
        return yield_fixture
    except Exception:
        if database_created:
            with psycopg.connect(maintenance_conninfo, autocommit=True) as connection:
                connection.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
                )
                connection.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name))
                )
        raise


@pytest.fixture(scope="module")
def phase8_business(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Phase8BusinessFixture]:
    fixture = _create_business_fixture(tmp_path_factory)
    try:
        yield fixture
    finally:
        password_path = os.getenv("EM_TEST_BUSINESS_PASSWORD_FILE")
        if password_path:
            maintenance_path = Path(
                os.getenv("EM_TEST_BUSINESS_MAINTENANCE_PASSWORD_FILE", password_path)
            )
            port = int(os.getenv("EM_TEST_BUSINESS_PORT", "55484"))
            conninfo = _maintenance_conninfo(maintenance_path, port)
            try:
                with psycopg.connect(conninfo, autocommit=True) as connection:
                    connection.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            sql.Identifier(fixture.database_name)
                        )
                    )
                    connection.execute(
                        sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(fixture.role_name))
                    )
            except psycopg.Error as error:
                raise RuntimeError("Phase-8 business fixture cleanup failed") from error


def _write_mcp_server_config(business: Phase8BusinessFixture, directory: Path, port: int) -> Path:
    path = directory / "database-mcp-server.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "database": {
                    "host": "127.0.0.1",
                    "port": business.port,
                    "database_name": business.database_name,
                    "user": business.role_name,
                    "password_file": str(business.role_password_file),
                    "sslmode": "disable",
                    "secret_ref": business.source_metadata["secret_ref"],
                    "connection_options": None,
                },
                "auth_secret_file": str(business.token_file),
                "listen_host": "127.0.0.1",
                "listen_port": port,
                "allowed_hosts": [f"127.0.0.1:{port}", "127.0.0.1", "localhost"],
                "allowed_origins": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def write_mcp_client_config(business: Phase8BusinessFixture, endpoint: str, path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "database_mcp": {
                    "endpoint": endpoint,
                    "auth_secret_file": str(business.token_file),
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def phase8_mcp(
    phase8_business: Phase8BusinessFixture, tmp_path: Path
) -> Iterator[Phase8MCPFixture]:
    port = free_port()
    server_config = _write_mcp_server_config(phase8_business, tmp_path, port)
    env = os.environ.copy()
    env["EM_DATABASE_MCP_SERVER_CONFIG"] = str(server_config)
    env["PYTHONUTF8"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-m", "enterprise_mind.database.mcp_server"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        wait_for_port(process, port)
        yield Phase8MCPFixture(
            business=phase8_business,
            endpoint=f"http://127.0.0.1:{port}/mcp",
            port=port,
            server_config_file=server_config,
        )
    finally:
        stop_process(process)


async def refresh_schema_once(settings: Any, endpoint: str, token: str, *, limit: int = 10) -> int:
    engine = create_engine(settings, ApplicationConfig(), role="worker")
    sessions = session_factory(engine)
    client = DatabaseMCPClient(endpoint, token)
    try:
        async with sessions() as session, session.begin():
            return await poll_schema_refresh_outbox(session, client.fetch_schema, limit=limit)
    finally:
        await client.close()
        await engine.dispose()


def refresh_schema(settings: Any, endpoint: str, token: str, *, limit: int = 10) -> int:
    with asyncio.Runner(loop_factory=agent_event_loop) as runner:
        return runner.run(refresh_schema_once(settings, endpoint, token, limit=limit))


def mcp_process_config(business: Phase8BusinessFixture) -> DatabaseConnectionConfig:
    return DatabaseConnectionConfig(
        host="127.0.0.1",
        port=business.port,
        database_name=business.database_name,
        user=business.role_name,
        password_file=business.role_password_file,
        sslmode="disable",
        secret_ref=business.source_metadata["secret_ref"],
    )


__all__ = [
    "Phase8BusinessFixture",
    "Phase8MCPFixture",
    "free_port",
    "mcp_process_config",
    "phase8_business",
    "phase8_mcp",
    "port_open",
    "refresh_schema",
    "stop_process",
    "wait_for_port",
    "write_mcp_client_config",
]
