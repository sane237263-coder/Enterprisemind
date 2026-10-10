import os
import secrets
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool

from enterprise_mind.app import create_app

pytestmark = pytest.mark.integration


def environment(database):
    env = os.environ.copy()
    env.update(
        EM_ENV="test",
        EM_POSTGRES_HOST=database.settings.postgres_host,
        EM_POSTGRES_PORT=str(database.settings.postgres_port),
        EM_POSTGRES_DATABASE=database.settings.postgres_database,
        EM_POSTGRES_USER=database.settings.postgres_user,
        EM_POSTGRES_PASSWORD_FILE=str(database.settings.postgres_password_file),
        EM_APPLICATION_CONFIG=str(database.settings.application_config),
        EM_TOKEN_SIGNING_KEY_FILE=str(database.settings.token_signing_key_file),
    )
    env.pop("EM_BOOTSTRAP_ADMIN_USERNAME", None)
    env.pop("EM_BOOTSTRAP_ADMIN_PASSWORD_FILE", None)
    return env


def test_cli_refuses_uninitialized_database_once_without_traceback(database):
    process = subprocess.run(
        [sys.executable, "-m", "enterprise_mind.cli", "serve"],
        env=environment(database),
        capture_output=True,
        timeout=15,
    )
    assert process.returncode == 1
    assert b"Traceback" not in process.stderr
    assert str(database.settings.postgres_password_file).encode() not in process.stderr


def test_real_http_server_login_with_bootstrap_secret_absent(database):
    database.initialize()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    # One worker here makes cross-platform process cleanup deterministic. The CLI preflight is
    # exercised separately; this smoke checks the actual Uvicorn socket and application factory.
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "enterprise_mind.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        env=environment(database),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False
        ) as client:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    pytest.fail("API process exited before becoming ready")
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() >= deadline:
                    pytest.fail("API did not become ready")
                time.sleep(0.1)
            response = client.post(
                "/api/v1/auth/login",
                json={"username": "初始管理员", "password": database.admin_password},
            )
            assert response.status_code == 200
            token = response.json()["data"]["access_token"]
            me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
            assert me.status_code == 200
            assert me.json()["data"]["role"] == "admin"
            headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "http-smoke"}
            first = client.post(
                "/api/v1/admin/departments", headers=headers, json={"name": "HTTP部门"}
            )
            assert first.status_code == 201
            retry = client.post(
                "/api/v1/admin/departments", headers=headers, json={"name": "HTTP部门"}
            )
            assert retry.status_code == 201 and retry.json()["data"] == first.json()["data"]
            listing = client.get("/api/v1/admin/departments", headers=headers)
            assert listing.json()["data"] == [first.json()["data"]]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_auth_works_with_nonowner_readonly_runtime_role(database, tmp_path):
    database.initialize()
    role = "em_test_role_" + uuid.uuid4().hex
    password = secrets.token_urlsafe(32)
    password_file = tmp_path / "runtime_password"
    password_file.write_text(password, encoding="utf-8")
    role_sql = sql.Identifier(role)
    # SQL identifiers and values are composed by psycopg; no credentials are logged.
    with database.engine.begin() as connection:
        cursor = connection.connection.driver_connection.cursor()
        cursor.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(role_sql, sql.Literal(password))
        )
        cursor.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database.url.database), role_sql
            )
        )
        cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA iam, infra TO {}").format(role_sql))
        cursor.execute(
            sql.SQL(
                "GRANT SELECT ON iam.users, iam.departments, infra.alembic_version TO {}"
            ).format(role_sql)
        )
    try:
        settings = database.settings.model_copy(
            update={
                "postgres_user": role,
                "postgres_password_file": password_file,
            }
        )
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/api/v1/auth/login",
                json={"username": "初始管理员", "password": database.admin_password},
            )
            assert response.status_code == 200
            assert client.get("/health/ready").status_code == 200
        restricted = create_engine(
            settings.database_url(), hide_parameters=True, poolclass=NullPool
        )
        try:
            with pytest.raises(ProgrammingError), restricted.begin() as connection:
                connection.execute(text("CREATE SCHEMA forbidden"))
            with pytest.raises(ProgrammingError), restricted.begin() as connection:
                connection.execute(text("UPDATE iam.users SET role='admin'"))
        finally:
            restricted.dispose()
    finally:
        with database.engine.begin() as connection:
            cursor = connection.connection.driver_connection.cursor()
            cursor.execute(sql.SQL("DROP OWNED BY {}").format(role_sql))
            cursor.execute(sql.SQL("DROP ROLE {}").format(role_sql))
