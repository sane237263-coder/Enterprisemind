import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_management import auth

from enterprise_mind.app import create_app
from enterprise_mind.database.contracts import SchemaCatalog, SchemaColumn, SchemaRelation
from enterprise_mind.database.models import (
    DatabaseSource,
    SchemaSnapshot,
)
from enterprise_mind.database.projection import authorized_schema
from enterprise_mind.database.service import process_schema_refresh, process_schema_refresh_outbox
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.models import OutboxEvent

pytestmark = pytest.mark.integration
PASSWORD = "database-admin-test-password"


@pytest.fixture
def database_app(database):
    database.initialize()
    with TestClient(
        create_app(database.settings),
        backend_options={"loop_factory": asyncio.SelectorEventLoop},
    ) as client:
        yield client, client.app


def _source_body(database, **overrides):
    body = {
        "name": "业务数据库",
        "host": "business-db",
        "port": 5432,
        "database_name": "business",
        "ssl_mode": "verify-full",
        "secret_ref": "secret://business-readonly",
    }
    body.update(overrides)
    return body


def _create_source(client, headers, database, key="source-create", **overrides):
    body = _source_body(database, **overrides)
    response = client.put(
        "/api/v1/admin/database-mcp/source",
        headers={**headers, "Idempotency-Key": key},
        json=body,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _error(response, code: str, status: int):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code, response.text


async def _refresh(app, snapshot_id: int, fetch):
    async with app.state.sessions() as session, session.begin():
        return await process_schema_refresh(session, snapshot_id, fetch)


def _schema_fetch(comment="订单元数据"):
    async def fetch(source):
        assert source.secret_ref == "secret://business-readonly"
        return SchemaCatalog(
            (
                SchemaRelation(
                    schema_name="biz",
                    relation_name="orders",
                    relation_type="table",
                    comment=comment,
                    relation_oid=123,
                    columns=(SchemaColumn("id", "bigint", ordinal=1),),
                ),
            )
        )

    return fetch


def test_database_source_api_is_idempotent_and_rejects_unsafe_or_second_source(
    database_app, database
):
    client, _ = database_app
    admin = auth(client, "初始管理员", database.admin_password)

    _error(
        client.put(
            "/api/v1/admin/database-mcp/source",
            headers={**admin, "Idempotency-Key": "self-db"},
            json=_source_body(database, database_name=database.settings.postgres_database),
        ),
        "VALIDATION_ERROR",
        422,
    )
    _error(
        client.put(
            "/api/v1/admin/database-mcp/source",
            headers={**admin, "Idempotency-Key": "unsafe-ref"},
            json=_source_body(database, secret_ref=r"C:\\secrets\\db-password"),
        ),
        "VALIDATION_ERROR",
        422,
    )

    source = _create_source(client, admin, database)
    replay = client.put(
        "/api/v1/admin/database-mcp/source",
        headers={**admin, "Idempotency-Key": "source-create"},
        json=_source_body(database),
    )
    assert replay.status_code == 201
    assert replay.json()["data"]["id"] == source["id"]
    assert "secret_ref" not in replay.text

    _error(
        client.put(
            "/api/v1/admin/database-mcp/source",
            headers={**admin, "Idempotency-Key": "source-second"},
            json=_source_body(database, name="another"),
        ),
        "DATABASE_SOURCE_ALREADY_CONFIGURED",
        409,
    )
    current = client.get("/api/v1/admin/database-mcp/source", headers=admin)
    assert current.status_code == 200
    assert "secret_ref" not in current.text
    assert current.json()["data"]["id"] == source["id"]


def test_schema_refresh_failure_preserves_active_and_newer_snapshot_wins(database_app, database):
    client, app = database_app
    admin = auth(client, "初始管理员", database.admin_password)
    source = _create_source(client, admin, database)

    first = client.post(
        "/api/v1/admin/database-mcp/schema-refresh",
        headers={**admin, "Idempotency-Key": "refresh-one"},
    )
    assert first.status_code == 202, first.text
    first_id = int(first.json()["data"]["id"])
    completed = client.portal.call(_refresh, app, first_id, _schema_fetch())
    assert completed.status == "active"

    failed_request = client.post(
        "/api/v1/admin/database-mcp/schema-refresh",
        headers={**admin, "Idempotency-Key": "refresh-fail"},
    )
    failed_id = int(failed_request.json()["data"]["id"])

    async def fail(_source):
        raise RuntimeError("private driver details must not persist")

    failed = client.portal.call(_refresh, app, failed_id, fail)
    assert failed.status == "failed"
    with Session(database.engine) as session:
        row = session.get(DatabaseSource, int(source["id"]))
        assert row is not None and row.active_snapshot_id == first_id
        assert session.get(SchemaSnapshot, first_id).status == "active"
        assert session.get(SchemaSnapshot, failed_id).status == "failed"

    second_request = client.post(
        "/api/v1/admin/database-mcp/schema-refresh",
        headers={**admin, "Idempotency-Key": "refresh-two"},
    )
    second_id = int(second_request.json()["data"]["id"])
    switched = client.portal.call(_refresh, app, second_id, _schema_fetch("新快照"))
    assert switched.status == "active"
    with Session(database.engine) as session:
        row = session.get(DatabaseSource, int(source["id"]))
        assert row is not None and row.active_snapshot_id == second_id
        assert session.get(SchemaSnapshot, first_id).status == "retired"
    for mutation in (
        "UPDATE integration.database_sources SET status='disabled'",
        "UPDATE integration.schema_snapshots SET status='retired' WHERE status='active'",
    ):
        with pytest.raises(IntegrityError), database.engine.begin() as connection:
            connection.execute(text(mutation))
    # A second click while a build is pending is intentionally coalesced.  It
    # must not create a second building snapshot merely because the caller
    # supplied another idempotency key.
    duplicate_one = client.post(
        "/api/v1/admin/database-mcp/schema-refresh",
        headers={**admin, "Idempotency-Key": "duplicate-one"},
    )
    duplicate_two = client.post(
        "/api/v1/admin/database-mcp/schema-refresh",
        headers={**admin, "Idempotency-Key": "duplicate-two"},
    )
    duplicate_replay = client.post(
        "/api/v1/admin/database-mcp/schema-refresh",
        headers={**admin, "Idempotency-Key": "duplicate-one"},
    )
    duplicate_ids = {
        int(duplicate_one.json()["data"]["id"]),
        int(duplicate_two.json()["data"]["id"]),
        int(duplicate_replay.json()["data"]["id"]),
    }
    assert (
        duplicate_one.status_code
        == duplicate_two.status_code
        == duplicate_replay.status_code
        == 202
    )
    assert len(duplicate_ids) == 1

    # The API coalescing rule means the service-level supersession race needs
    # two explicitly isolated building rows.  Seed them with consecutive
    # snapshot numbers, activate the newer one first, then prove the older
    # completion is retired without replacing the active pointer.
    with Session(database.engine) as session:
        source_row = session.get(DatabaseSource, int(source["id"]))
        assert source_row is not None
        latest_no = session.scalar(
            select(func.max(SchemaSnapshot.snapshot_no)).where(
                SchemaSnapshot.database_source_id == source_row.id
            )
        )
        older = SchemaSnapshot(
            database_source_id=source_row.id,
            snapshot_no=int(latest_no or 0) + 1,
            status="building",
        )
        newer = SchemaSnapshot(
            database_source_id=source_row.id,
            snapshot_no=int(latest_no or 0) + 2,
            status="building",
        )
        session.add_all([older, newer])
        session.flush()
        older_id, newer_id = older.id, newer.id
        session.commit()

    assert client.portal.call(_refresh, app, newer_id, _schema_fetch("更新快照")).status == "active"
    older_result = client.portal.call(_refresh, app, older_id, _schema_fetch("旧快照"))
    assert older_result.status == "retired"
    assert older_result.error_code == "SCHEMA_REFRESH_SUPERSEDED"
    with Session(database.engine) as session:
        assert session.get(DatabaseSource, int(source["id"])).active_snapshot_id == newer_id


def test_schema_refresh_outbox_cancel_rolls_back_and_can_reclaim(database_app, database):
    client, app = database_app
    admin = auth(client, "初始管理员", database.admin_password)
    _create_source(client, admin, database)
    response = client.post(
        "/api/v1/admin/database-mcp/schema-refresh",
        headers={**admin, "Idempotency-Key": "cancel-refresh"},
    )
    snapshot_id = int(response.json()["data"]["id"])

    async def interrupted(_source):
        raise asyncio.CancelledError

    async def cancelled_worker():
        try:
            async with app.state.sessions() as session, session.begin():
                await process_schema_refresh_outbox(session, interrupted)
        except asyncio.CancelledError:
            return True
        return False

    assert client.portal.call(cancelled_worker)
    with Session(database.engine) as session:
        assert session.get(SchemaSnapshot, snapshot_id).status == "building"
        assert session.scalars(select(OutboxEvent)).one().status == "pending"

    async def replay():
        async with app.state.sessions() as session, session.begin():
            return await process_schema_refresh_outbox(session, _schema_fetch())

    assert client.portal.call(replay).status == "active"
    with Session(database.engine) as session:
        event = session.scalars(select(OutboxEvent)).one()
        assert event.status == "published" and event.attempt_count == 1


def test_policy_projection_is_department_scoped_and_revocation_is_immediate(database_app, database):
    client, app = database_app
    admin = auth(client, "初始管理员", database.admin_password)
    department_a = client.post(
        "/api/v1/admin/departments",
        headers={**admin, "Idempotency-Key": "database-dept-a"},
        json={"name": "业务甲"},
    ).json()["data"]["id"]
    department_b = client.post(
        "/api/v1/admin/departments",
        headers={**admin, "Idempotency-Key": "database-dept-b"},
        json={"name": "业务乙"},
    ).json()["data"]["id"]
    for key, username, department_id in (
        ("database-user-a", "数据库甲用户", department_a),
        ("database-user-b", "数据库乙用户", department_b),
    ):
        created = client.post(
            "/api/v1/admin/users",
            headers={**admin, "Idempotency-Key": key},
            json={
                "username": username,
                "password": PASSWORD,
                "role": "user",
                "department_id": department_id,
            },
        )
        assert created.status_code == 201, created.text

    _create_source(client, admin, database)
    refresh = client.post(
        "/api/v1/admin/database-mcp/schema-refresh",
        headers={**admin, "Idempotency-Key": "projection-refresh"},
    )
    snapshot_id = int(refresh.json()["data"]["id"])
    client.portal.call(_refresh, app, snapshot_id, _schema_fetch())

    policy = client.put(
        "/api/v1/admin/database-mcp/relation-policies",
        headers=admin,
        json={
            "schema_name": "biz",
            "relation_name": "orders",
            "enabled": True,
            "department_ids": [department_a],
        },
    )
    assert policy.status_code == 200, policy.text
    assert policy.json()["data"]["row_version"] == 1

    async def keys_for(username):
        async with app.state.sessions() as session:
            user, department = (
                await session.execute(
                    select(User, Department)
                    .outerjoin(Department, User.department_id == Department.id)
                    .where(User.username == username)
                )
            ).one()
            binding = await authorized_schema(
                session,
                CurrentIdentity(user, department, user.credential_version),
            )
            return binding.relation_keys

    assert client.portal.call(keys_for, "数据库甲用户") == {("biz", "orders")}
    assert client.portal.call(keys_for, "数据库乙用户") == set()
    assert client.portal.call(keys_for, "初始管理员") == {("biz", "orders")}

    revoked = client.put(
        "/api/v1/admin/database-mcp/relation-policies",
        headers=admin,
        json={
            "schema_name": "biz",
            "relation_name": "orders",
            "enabled": False,
            "department_ids": [],
            "row_version": 1,
        },
    )
    assert revoked.status_code == 200, revoked.text
    assert client.portal.call(keys_for, "数据库甲用户") == set()


def test_phase10_p8_r1_representative_admin_reads_repeat_without_internal_errors(
    database_app, database, caplog
):
    """Replay the historical management page read flow ten times.

    This is deliberately a deterministic API regression.  The live browser
    observation still owns the P8-R1 conclusion, while the middleware emits a
    redacted diagnostic if an unexpected 500 recurs.
    """

    client, app = database_app
    admin = auth(client, "初始管理员", database.admin_password)
    _create_source(client, admin, database, key="p8-r1-source")
    refresh = client.post(
        "/api/v1/admin/database-mcp/schema-refresh",
        headers={**admin, "Idempotency-Key": "p8-r1-refresh"},
    )
    assert refresh.status_code == 202, refresh.text
    snapshot_id = int(refresh.json()["data"]["id"])
    assert client.portal.call(_refresh, app, snapshot_id, _schema_fetch()).status == "active"

    paths = (
        "/api/v1/admin/users",
        "/api/v1/admin/database-mcp/source",
        "/api/v1/admin/database-mcp/schema",
        "/api/v1/admin/database-mcp/relation-policies",
        "/api/v1/admin/database-mcp/schema-refresh/latest",
        f"/api/v1/admin/database-mcp/schema-refreshes/{snapshot_id}",
    )
    observations: list[dict[str, object]] = []
    for _ in range(10):
        for path in paths:
            response = client.get(path, headers=admin)
            observations.append(
                {
                    "path": path.split("?", 1)[0],
                    "status": response.status_code,
                    "request_id": response.headers.get("x-request-id"),
                }
            )
            assert response.status_code == 200, response.text

    # Exercise a small concurrent read pair after the sequential warm-up.
    from concurrent.futures import ThreadPoolExecutor

    concurrent_paths = (
        "/api/v1/admin/database-mcp/schema",
        "/api/v1/admin/database-mcp/relation-policies",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda path: client.get(path, headers=admin), concurrent_paths))
    observations.extend(
        {
            "path": path,
            "status": response.status_code,
            "request_id": response.headers.get("x-request-id"),
        }
        for path, response in zip(concurrent_paths, responses, strict=True)
    )
    assert all(item["status"] == 200 for item in observations)
    assert len(observations) == 62
    assert all(isinstance(item["request_id"], str) for item in observations)
    assert "api_request_failed" not in caplog.text
