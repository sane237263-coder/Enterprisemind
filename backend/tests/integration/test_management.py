import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from enterprise_mind.app import create_app
from enterprise_mind.identity.management import ORGANIZATION_LOCK
from enterprise_mind.identity.models import User
from enterprise_mind.infrastructure.models import IdempotencyRecord
from enterprise_mind.knowledge.models import DeletionBatch, KnowledgeBase

pytestmark = pytest.mark.integration
D = "/api/v1/admin/departments"
U = "/api/v1/admin/users"
K = "/api/v1/knowledge-bases"
PASSWORD = "test-management-password-123"


def auth(client, username, password):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["data"]["access_token"]}


@contextmanager
def client_for(database):
    database.initialize()
    with TestClient(create_app(database.settings)) as client:
        yield client, auth(client, "初始管理员", database.admin_password)


def create(client, headers, path, body, key=None):
    return client.post(
        path, headers={**headers, "Idempotency-Key": key or uuid.uuid4().hex}, json=body
    )


def created(client, headers, path, body, key=None):
    response = create(client, headers, path, body, key)
    assert response.status_code == 201, response.text
    assert response.json()["meta"]["request_id"] == response.headers["x-request-id"]
    return response.json()["data"]


def setup_organization(client, headers):
    a = created(client, headers, D, {"name": "甲部门"})
    b = created(client, headers, D, {"name": "乙部门"})
    user = created(
        client,
        headers,
        U,
        {"username": "普通用户", "password": PASSWORD, "role": "user", "department_id": a["id"]},
    )
    return a, b, user, auth(client, "普通用户", PASSWORD)


def code(response, expected, status):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == expected, response.text


def test_user_crud_versions_password_reset_and_immediate_invalidation(database):
    with client_for(database) as (client, admin):
        a, b, user, ordinary = setup_organization(client, admin)
        target = U + "/" + user["id"]
        result = client.patch(
            target,
            headers=admin,
            json={"row_version": 1, "username": "更新用户", "department_id": b["id"]},
        )
        assert result.status_code == 200
        assert result.json()["data"]["row_version"] == 2
        assert result.json()["data"]["department"]["id"] == b["id"]
        code(
            client.patch(target, headers=admin, json={"row_version": 1, "status": "disabled"}),
            "ROW_VERSION_CONFLICT",
            409,
        )
        assert client.get(target, headers=admin).json()["data"]["status"] == "active"
        reset = target + "/password-reset"
        code(
            client.post(reset, headers=admin, json={"row_version": 2, "new_password": "short"}),
            "VALIDATION_ERROR",
            422,
        )
        assert client.get("/api/v1/auth/me", headers=ordinary).status_code == 200
        code(
            client.post(
                reset, headers=admin, json={"row_version": 1, "new_password": PASSWORD + "2"}
            ),
            "ROW_VERSION_CONFLICT",
            409,
        )
        response = client.post(
            reset, headers=admin, json={"row_version": 2, "new_password": PASSWORD + "2"}
        )
        assert response.status_code == 200
        assert response.json()["data"]["row_version"] == 3
        assert "password" not in response.text and "credential_version" not in response.text
        code(client.get("/api/v1/auth/me", headers=ordinary), "AUTH_REQUIRED", 401)
        code(
            client.post("/api/v1/auth/login", json={"username": "更新用户", "password": PASSWORD}),
            "AUTH_INVALID_CREDENTIALS",
            401,
        )
        fresh = auth(client, "更新用户", PASSWORD + "2")
        assert (
            client.patch(
                target, headers=admin, json={"row_version": 3, "status": "disabled"}
            ).status_code
            == 200
        )
        code(client.get("/api/v1/auth/me", headers=fresh), "ACCOUNT_DISABLED", 403)
        assert (
            client.patch(
                target, headers=admin, json={"row_version": 4, "status": "active"}
            ).status_code
            == 200
        )
        assert client.get("/api/v1/auth/me", headers=fresh).status_code == 200
        assert client.delete(target, headers=admin).status_code == 405
    with Session(database.engine) as session:
        saved = session.get(User, int(user["id"]))
        assert saved.credential_version == 2
        assert saved.password_hash.startswith("$argon2id$")


def test_admin_password_reset_self_commits_then_rejects_old_token(database):
    with client_for(database) as (client, admin):
        me = client.get("/api/v1/auth/me", headers=admin).json()["data"]
        response = client.post(
            U + "/" + me["id"] + "/password-reset",
            headers=admin,
            json={"row_version": 1, "new_password": PASSWORD},
        )
        assert response.status_code == 200
        code(client.get("/api/v1/auth/me", headers=admin), "AUTH_REQUIRED", 401)
        assert auth(client, "初始管理员", PASSWORD)


def test_department_disable_and_admin_optional_department(database):
    with client_for(database) as (client, admin):
        a, b, user, ordinary = setup_organization(client, admin)
        admin_id = client.get("/api/v1/auth/me", headers=admin).json()["data"]["id"]
        assert (
            client.patch(
                U + "/" + admin_id, headers=admin, json={"row_version": 1, "department_id": a["id"]}
            ).status_code
            == 200
        )
        assert (
            client.patch(
                D + "/" + a["id"], headers=admin, json={"row_version": 1, "status": "disabled"}
            ).status_code
            == 200
        )
        code(client.get(K, headers=ordinary), "DEPARTMENT_DISABLED", 403)
        code(
            client.post("/api/v1/auth/login", json={"username": "普通用户", "password": PASSWORD}),
            "AUTH_INVALID_CREDENTIALS",
            401,
        )
        assert client.get(D, headers=admin).status_code == 200
        assert auth(client, "初始管理员", database.admin_password)
        assert (
            client.patch(
                D + "/" + a["id"], headers=admin, json={"row_version": 2, "status": "active"}
            ).status_code
            == 200
        )
        assert client.get(K, headers=ordinary).status_code == 200


def test_department_delete_checks_version_and_all_references(database):
    with client_for(database) as (client, admin):
        a, b, user, ordinary = setup_organization(client, admin)
        code(
            client.delete(D + "/" + a["id"], headers=admin, params={"row_version": 1}),
            "DEPARTMENT_IN_USE",
            409,
        )
        created(client, admin, K, {"name": "乙库", "type": "department", "department_id": b["id"]})
        code(
            client.delete(D + "/" + b["id"], headers=admin, params={"row_version": 1}),
            "DEPARTMENT_IN_USE",
            409,
        )
        empty = created(client, admin, D, {"name": "空部门"}, "delete-replay")
        code(
            client.delete(D + "/" + empty["id"], headers=admin, params={"row_version": 2}),
            "ROW_VERSION_CONFLICT",
            409,
        )
        response = client.delete(D + "/" + empty["id"], headers=admin, params={"row_version": 1})
        assert response.status_code == 204 and response.content == b""
        code(client.get(D + "/" + empty["id"], headers=admin), "RESOURCE_NOT_FOUND", 404)
        assert created(client, admin, D, {"name": "空部门"}, "delete-replay") == empty
        assert client.get(D, headers=admin).json()["data"] == [a, b]


def test_knowledge_visibility_transfer_naming_and_root_permissions(database):
    with client_for(database) as (client, admin):
        a, b, user, ordinary = setup_organization(client, admin)
        public = created(client, admin, K, {"name": "同名", "type": "public"})
        own = created(
            client, admin, K, {"name": "同名", "type": "department", "department_id": a["id"]}
        )
        other = created(
            client, admin, K, {"name": "同名", "type": "department", "department_id": b["id"]}
        )
        assert [x["id"] for x in client.get(K, headers=ordinary).json()["data"]] == [
            public["id"],
            own["id"],
        ]
        code(client.get(K + "/" + other["id"], headers=ordinary), "RESOURCE_NOT_FOUND", 404)
        code(
            create(client, ordinary, K, {"name": "越权", "type": "public"}),
            "PERMISSION_DENIED",
            403,
        )
        code(
            client.patch(
                K + "/" + own["id"], headers=ordinary, json={"row_version": 1, "name": "越权"}
            ),
            "PERMISSION_DENIED",
            403,
        )
        code(
            client.patch(
                K + "/" + own["id"],
                headers=admin,
                json={"row_version": 1, "department_id": b["id"]},
            ),
            "NAME_CONFLICT",
            409,
        )
        assert client.get(K + "/" + own["id"], headers=admin).json()["data"] == own
        response = client.patch(
            K + "/" + own["id"],
            headers=admin,
            json={
                "row_version": 1,
                "department_id": b["id"],
                "name": "迁移库",
                "description": "新描述",
            },
        )
        assert response.status_code == 200
        code(client.get(K + "/" + own["id"], headers=ordinary), "RESOURCE_NOT_FOUND", 404)
        assert (
            client.patch(
                U + "/" + user["id"],
                headers=admin,
                json={"row_version": 1, "department_id": b["id"]},
            ).status_code
            == 200
        )
        assert client.get(K + "/" + own["id"], headers=ordinary).status_code == 200
        code(
            client.patch(
                K + "/" + public["id"],
                headers=admin,
                json={"row_version": 1, "department_id": b["id"]},
            ),
            "VALIDATION_ERROR",
            422,
        )
        code(
            client.patch(
                K + "/" + own["id"], headers=admin, json={"row_version": 1, "name": "过期"}
            ),
            "ROW_VERSION_CONFLICT",
            409,
        )
        assert client.delete(K + "/" + own["id"], headers=admin).status_code == 405


@pytest.mark.parametrize(
    "path, body",
    [
        (D, {"name": "", "parent_id": "1"}),
        (D, {"name": "\u200b"}),
        (U, {"username": "普通", "role": "user", "password": PASSWORD}),
        (U, {"username": "普通", "role": "user", "password": PASSWORD, "department_id": 1}),
        (
            U,
            {
                "username": "越权字段",
                "role": "admin",
                "password": PASSWORD,
                "credential_version": 1,
            },
        ),
        (K, {"name": "库", "type": "department"}),
        (K, {"name": "库", "type": "public", "department_id": "1"}),
        (K, {"name": "库", "type": "public", "status": "active"}),
    ],
)
def test_create_rejects_unconfirmed_fields_and_invalid_relations(database, path, body):
    with client_for(database) as (client, admin):
        response = create(client, admin, path, body)
        code(response, "VALIDATION_ERROR", 422)
        assert PASSWORD not in response.text
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0


def test_pagination_filtering_cursor_binding_and_normalized_uniqueness(database):
    with client_for(database) as (client, admin):
        first = created(client, admin, D, {"name": "Ｒ＆Ｄ"})
        code(create(client, admin, D, {"name": " r&d "}), "NAME_CONFLICT", 409)
        second = created(client, admin, D, {"name": "二部", "status": "disabled"})
        third = created(client, admin, D, {"name": "三部"})
        page = client.get(D, headers=admin, params={"limit": 1}).json()
        assert page["data"] == [first]
        cursor = page["meta"]["next_cursor"]
        rest = client.get(D, headers=admin, params={"limit": 100, "cursor": cursor}).json()
        assert rest["data"] == [second, third] and rest["meta"]["next_cursor"] is None
        assert client.get(D, headers=admin, params={"status": "disabled"}).json()["data"] == [
            second
        ]
        code(
            client.get(D, headers=admin, params={"status": "disabled", "cursor": cursor}),
            "VALIDATION_ERROR",
            422,
        )
        code(client.get(K, headers=admin, params={"cursor": cursor}), "VALIDATION_ERROR", 422)
        for bad in ("garbage", cursor + "tampered"):
            code(client.get(D, headers=admin, params={"cursor": bad}), "VALIDATION_ERROR", 422)
        for bad in ("0", "01", "-1", "9223372036854775808"):
            code(client.get(D + "/" + bad, headers=admin), "VALIDATION_ERROR", 422)


def test_idempotency_concurrent_retry_restart_conflict_and_safe_storage(database):
    with client_for(database) as (client, admin):
        body = {"username": "重试用户", "password": PASSWORD, "role": "admin"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create, client, admin, U, body, "same-key") for _ in range(2)]
            responses = [future.result() for future in futures]
        assert [r.status_code for r in responses] == [201, 201]
        result = responses[0].json()["data"]
        assert result == responses[1].json()["data"]
        assert (
            client.patch(
                U + "/" + result["id"], headers=admin, json={"row_version": 1, "username": "已改名"}
            ).status_code
            == 200
        )
        code(
            create(client, admin, U, {**body, "password": PASSWORD + "x"}, "same-key"),
            "IDEMPOTENCY_KEY_CONFLICT",
            409,
        )
        code(
            create(client, admin, D, {"name": "部门"}, "same-key"), "IDEMPOTENCY_KEY_CONFLICT", 409
        )
    with TestClient(create_app(database.settings)) as restarted:
        assert created(restarted, admin, U, body, "same-key") == result
        second_admin = auth(restarted, "已改名", PASSWORD)
        # Keys are scoped to the authenticated actor, even across operation types.
        created(restarted, second_admin, D, {"name": "独立键"}, "same-key")
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(User)) == 2
        records = session.scalars(select(IdempotencyRecord)).all()
        assert len(records) == 2
        for record in records:
            stored = json.dumps(record.response_data)
            assert all(
                secret not in stored
                for secret in (PASSWORD, "password_hash", "credential_version", "access_token")
            )
            assert len(record.request_fingerprint) == 64


def test_failed_create_does_not_consume_key_and_replay_rechecks_admin(database):
    with client_for(database) as (client, admin):
        a, b, user, ordinary = setup_organization(client, admin)
        code(
            create(
                client,
                admin,
                U,
                {"username": "弱口令", "password": "short", "role": "admin"},
                "retry-failure",
            ),
            "VALIDATION_ERROR",
            422,
        )
        created(
            client,
            admin,
            U,
            {"username": "弱口令", "password": PASSWORD, "role": "admin"},
            "retry-failure",
        )
        actor = created(
            client, admin, U, {"username": "临时管理员", "password": PASSWORD, "role": "admin"}
        )
        actor_headers = auth(client, "临时管理员", PASSWORD)
        created(client, actor_headers, D, {"name": "已创建"}, "replay-role")
        assert (
            client.patch(
                U + "/" + actor["id"],
                headers=admin,
                json={"row_version": 1, "role": "user", "department_id": a["id"]},
            ).status_code
            == 200
        )
        code(
            create(client, actor_headers, D, {"name": "已创建"}, "replay-role"),
            "PERMISSION_DENIED",
            403,
        )
        for path in (D, U):
            code(client.get(path, headers=ordinary), "PERMISSION_DENIED", 403)


def test_concurrent_updates_only_one_wins(database):
    with client_for(database) as (client, admin):
        department = created(client, admin, D, {"name": "并发"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    client.patch,
                    D + "/" + department["id"],
                    headers=admin,
                    json={"row_version": 1, "name": name},
                )
                for name in ("更新甲", "更新乙")
            ]
            responses = [future.result() for future in futures]
        assert sorted(r.status_code for r in responses) == [200, 409]
        assert (
            client.get(D + "/" + department["id"], headers=admin).json()["data"]["row_version"] == 2
        )


def test_recycled_knowledge_is_invisible_and_blocks_department_delete(database):
    with client_for(database) as (client, admin):
        department = created(client, admin, D, {"name": "回收引用"})
        resource = created(
            client,
            admin,
            K,
            {"name": "回收库", "type": "department", "department_id": department["id"]},
        )
        # No deletion API is introduced in this stage. Seed the documented state directly.
        with Session(database.engine) as session, session.begin():
            now = datetime.now(UTC)
            batch = DeletionBatch(
                root_type="knowledge_base",
                root_id=int(resource["id"]),
                root_name="回收库",
                deleted_at=now,
                purge_after=now + timedelta(days=30),
            )
            session.add(batch)
            session.flush()
            saved = session.get(KnowledgeBase, int(resource["id"]))
            saved.deleted_at, saved.purge_after, saved.deletion_batch_id = (
                now,
                batch.purge_after,
                batch.id,
            )
        assert client.get(K, headers=admin).json()["data"] == []
        code(client.get(K + "/" + resource["id"], headers=admin), "RESOURCE_NOT_FOUND", 404)
        code(
            client.delete(D + "/" + department["id"], headers=admin, params={"row_version": 1}),
            "DEPARTMENT_IN_USE",
            409,
        )
        created(
            client,
            admin,
            K,
            {"name": "回收库", "type": "department", "department_id": department["id"]},
        )


def test_password_reset_commit_failure_returns_error_and_rolls_back(database):
    with client_for(database) as (client, admin):
        me = client.get("/api/v1/auth/me", headers=admin).json()["data"]
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE FUNCTION iam.reject_commit() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RAISE EXCEPTION 'test commit failure'; END $$"
                )
            )
            connection.execute(
                text(
                    "CREATE CONSTRAINT TRIGGER test_commit_failure AFTER UPDATE ON iam.users "
                    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                    "EXECUTE FUNCTION iam.reject_commit()"
                )
            )
        response = client.post(
            U + "/" + me["id"] + "/password-reset",
            headers=admin,
            json={"row_version": 1, "new_password": PASSWORD},
        )
        code(response, "INTERNAL_ERROR", 500)
        assert PASSWORD not in response.text and "test commit failure" not in response.text
        assert client.get("/api/v1/auth/me", headers=admin).status_code == 200
        assert auth(client, "初始管理员", database.admin_password)
        with Session(database.engine) as session:
            user = session.get(User, int(me["id"]))
            assert user.row_version == user.credential_version == 1


def test_creation_commit_failure_rolls_back_resource_and_idempotency_record(database):
    with client_for(database) as (client, admin):
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE FUNCTION infra.reject_creation() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RAISE EXCEPTION 'test commit failure'; END $$"
                )
            )
            connection.execute(
                text(
                    "CREATE CONSTRAINT TRIGGER reject_creation "
                    "AFTER INSERT ON infra.idempotency_records DEFERRABLE INITIALLY DEFERRED "
                    "FOR EACH ROW EXECUTE FUNCTION infra.reject_creation()"
                )
            )
        code(
            create(client, admin, D, {"name": "事务回滚"}, "commit-failure"), "INTERNAL_ERROR", 500
        )
        with database.engine.begin() as connection:
            assert connection.scalar(text("SELECT count(*) FROM iam.departments")) == 0
            assert connection.scalar(text("SELECT count(*) FROM infra.idempotency_records")) == 0
            connection.execute(text("DROP TRIGGER reject_creation ON infra.idempotency_records"))
        created(client, admin, D, {"name": "事务回滚"}, "commit-failure")


@pytest.mark.parametrize(
    "change, expected, status",
    [
        ("status='disabled'", "ACCOUNT_DISABLED", 403),
        ("credential_version=credential_version+1", "AUTH_REQUIRED", 401),
    ],
)
def test_queued_mutation_rechecks_identity_after_lock(database, change, expected, status):
    with client_for(database) as (client, admin):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with database.engine.begin() as connection:
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": ORGANIZATION_LOCK}
                )
                future = pool.submit(
                    create, client, admin, D, {"name": "等待鉴权"}, "queued-create"
                )
                deadline = time.monotonic() + 5
                waiting = False
                while time.monotonic() < deadline:
                    with database.engine.connect() as observer:
                        waiting = bool(
                            observer.scalar(
                                text(
                                    "SELECT 1 FROM pg_stat_activity "
                                    "WHERE datname=current_database() "
                                    "AND application_name='enterprise-mind-api' "
                                    "AND wait_event='advisory' LIMIT 1"
                                )
                            )
                        )
                    if waiting:
                        break
                    time.sleep(0.02)
                assert waiting
                # Fixed test-only SQL fragments above; never request input.
                connection.execute(text("UPDATE iam.users SET " + change))
            code(future.result(timeout=10), expected, status)
        with Session(database.engine) as session:
            assert session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0
