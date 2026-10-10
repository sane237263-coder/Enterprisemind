from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from enterprise_mind.app import create_app
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.security import Passwords
from enterprise_mind.infrastructure.database import DatabaseNotReady
from enterprise_mind.infrastructure.settings import AuthConfig

pytestmark = pytest.mark.integration


@contextmanager
def initialized_client(database):
    database.initialize()
    with TestClient(create_app(database.settings)) as client:
        yield client


def login(client, database, username="初始管理员", password=None):
    return client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password or database.admin_password},
    )


def authorization(client, database):
    response = login(client, database)
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def test_login_me_and_health_contract(database):
    with initialized_client(database) as client:
        response = login(client, database)
        assert response.status_code == 200
        payload = response.json()
        assert payload["meta"]["request_id"] == response.headers["x-request-id"]
        assert payload["data"]["expires_in"] == 1800
        assert response.headers["cache-control"] == "no-store"
        assert payload["data"]["user"]["id"].isdecimal()
        assert payload["data"]["user"]["created_at"].endswith("Z")
        assert payload["data"]["user"]["department"] is None
        me = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {payload['data']['access_token']}"},
        )
        assert me.status_code == 200
        assert me.headers["cache-control"] == "no-store"
        assert me.json()["data"] == payload["data"]["user"]
        for forbidden in ("password_hash", "credential_version", database.admin_password):
            assert forbidden not in response.text
            assert forbidden not in me.text
        assert client.get("/health/live").json()["status"] == "alive"
        assert client.get("/health/ready").json() == {
            "status": "ready",
            "stage": "organization_foundation",
        }


def test_login_failure_does_not_disclose_account_existence_or_disabled_state(database):
    with initialized_client(database) as client:
        responses = [
            login(client, database, username="不存在"),
            login(client, database, password="incorrect-password"),
        ]
        with database.engine.begin() as connection:
            connection.execute(text("UPDATE iam.users SET status='disabled'"))
        responses.append(login(client, database))
        for response in responses:
            assert response.status_code == 401
            error = response.json()["error"]
            assert error["code"] == "AUTH_INVALID_CREDENTIALS"
            assert error["message"] == "用户名或密码错误。"
            assert error["details"] == {}


def test_old_token_rechecks_status_credentials_and_current_role(database):
    with initialized_client(database) as client:
        headers = authorization(client, database)
        with database.engine.begin() as connection:
            connection.execute(text("UPDATE iam.users SET status='disabled'"))
        response = client.get("/api/v1/auth/me", headers=headers)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"
        with database.engine.begin() as connection:
            connection.execute(text("UPDATE iam.users SET status='active', credential_version=2"))
        assert (
            client.get("/api/v1/auth/me", headers=headers).json()["error"]["code"]
            == "AUTH_REQUIRED"
        )
        fresh = authorization(client, database)
        with Session(database.engine) as session, session.begin():
            department = Department(name="当前部门", name_key="当前部门")
            session.add(department)
            session.flush()
            admin = session.scalars(select(User)).one()
            admin.role, admin.department_id = "user", department.id
        response = client.get("/api/v1/auth/me", headers=fresh)
        assert response.json()["data"]["role"] == "user"
        assert response.json()["data"]["department"]["name"] == "当前部门"


def test_department_disabled_blocks_member_but_not_admin(database):
    with initialized_client(database) as client:
        with Session(database.engine) as session, session.begin():
            department = Department(name="研发", name_key="研发")
            session.add(department)
            session.flush()
            admin = session.scalars(select(User)).one()
            admin.department_id = department.id
            session.add(
                User(
                    username="成员",
                    username_key="成员",
                    role="user",
                    department_id=department.id,
                    password_hash=Passwords(AuthConfig()).hash(SecretStr(database.admin_password)),
                )
            )
        member = login(client, database, username="成员").json()["data"]["access_token"]
        admin_headers = authorization(client, database)
        with database.engine.begin() as connection:
            connection.execute(text("UPDATE iam.departments SET status='disabled'"))
        response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {member}"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "DEPARTMENT_DISABLED"
        assert (
            login(client, database, username="成员").json()["error"]["code"]
            == "AUTH_INVALID_CREDENTIALS"
        )
        assert client.get("/api/v1/auth/me", headers=admin_headers).status_code == 200
        assert login(client, database).status_code == 200


def test_invalid_requests_are_safe_and_new_routes_are_not_exposed(database, caplog):
    with initialized_client(database) as client:
        for payload in (
            {"username": "初始管理员", "password": database.admin_password, "role": "admin"},
            {"username": "初始管理员", "password": {"secret": database.admin_password}},
        ):
            response = client.post("/api/v1/auth/login", json=payload)
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "VALIDATION_ERROR"
            assert database.admin_password not in response.text
        malformed = client.post(
            "/api/v1/auth/login",
            content='{"password":',
            headers={"Content-Type": "application/json"},
        )
        assert malformed.status_code == 400
        assert malformed.json()["error"]["code"] == "INVALID_REQUEST"
        for headers in ({}, {"Authorization": "Bearer bad"}, {"Authorization": "Basic bad"}):
            assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
        for path in (
            "/api/v1/auth/register",
            "/api/v1/auth/refresh",
            "/api/v1/auth/password-change",
        ):
            assert client.post(path).status_code == 404
        assert database.admin_password not in caplog.text


def test_schema_mismatch_prevents_startup_and_readiness(database):
    database.initialize()
    with TestClient(create_app(database.settings)) as client:
        with database.engine.begin() as connection:
            connection.execute(text("UPDATE infra.alembic_version SET version_num='unrecognized'"))
        assert client.get("/health/ready").status_code == 503
        assert client.get("/health/live").status_code == 200
    with pytest.raises(DatabaseNotReady), TestClient(create_app(database.settings)):
        pass


def test_api_cannot_start_or_create_tables_in_empty_database(database):
    with pytest.raises(DatabaseNotReady), TestClient(create_app(database.settings)):
        pass
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT to_regclass('iam.users')")) is None


def test_username_login_is_unicode_normalized(database):
    database.initialize()
    with database.engine.begin() as connection:
        connection.execute(text("UPDATE iam.users SET username='Admin', username_key='admin'"))
    with TestClient(create_app(database.settings)) as client:
        assert login(client, database, username="  ＡＤＭＩＮ  ").status_code == 200
