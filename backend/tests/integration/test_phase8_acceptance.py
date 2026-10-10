"""Independent phase-eight acceptance of configuration and authorization changes."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_database_admin import _create_source, _refresh
from test_database_admin import database_app as database_app
from test_management import auth

from enterprise_mind.api.errors import APIError
from enterprise_mind.database.contracts import (
    SchemaCatalog,
    SchemaColumn,
    SchemaConstraint,
    SchemaRelation,
)
from enterprise_mind.database.models import DatabaseSource, RelationDepartmentGrant, RelationPolicy
from enterprise_mind.database.projection import (
    authorized_schema,
    detail_projection,
    revalidate_schema,
)
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.service import CurrentIdentity

pytestmark = pytest.mark.integration


def catalog():
    columns = (SchemaColumn("id", "bigint", ordinal=1, is_nullable=False),)
    return SchemaCatalog(
        (
            SchemaRelation(
                "biz",
                "orders",
                columns=columns,
                relation_oid=111,
                constraints=(SchemaConstraint("orders_pk", "primary_key", ("id",)),),
            ),
            SchemaRelation("finance", "payroll", columns=columns, relation_oid=222),
            SchemaRelation(
                "biz",
                "safe_orders",
                relation_type="view",
                columns=columns,
                relation_oid=333,
                view_definition_signature="a" * 64,
            ),
        )
    )


def publish(client, app, admin, value, key):
    response = client.post(
        "/api/v1/admin/database-mcp/schema-refresh",
        headers={**admin, "Idempotency-Key": key},
    )
    assert response.status_code == 202

    async def fetch(_source):
        return value

    result = client.portal.call(_refresh, app, int(response.json()["data"]["id"]), fetch)
    assert result.status == "active"
    return result.id


def policy(client, admin, name, departments, *, enabled=True, version=None):
    schema_name, relation_name = name
    body = {
        "schema_name": schema_name,
        "relation_name": relation_name,
        "enabled": enabled,
        "department_ids": departments,
    }
    if version is not None:
        body["row_version"] = version
    return client.put("/api/v1/admin/database-mcp/relation-policies", headers=admin, json=body)


async def binding(app, username):
    async with app.state.sessions() as session:
        user = await session.scalar(select(User).where(User.username == username))
        department = (
            await session.get(Department, user.department_id) if user.department_id else None
        )
        identity = CurrentIdentity(user, department, user.credential_version)
        return await authorized_schema(session, identity)


async def verify_binding(app, username, prior):
    async with app.state.sessions() as session:
        user = await session.scalar(select(User).where(User.username == username))
        department = (
            await session.get(Department, user.department_id) if user.department_id else None
        )
        identity = CurrentIdentity(user, department, user.credential_version)
        await revalidate_schema(
            session,
            identity,
            prior,
            tuple(relation.quoted_qualified_name for relation in prior.catalog.relations),
        )


@pytest.fixture
def configured(database_app, database):
    client, app = database_app
    admin = auth(client, "初始管理员", database.admin_password)
    source = _create_source(client, admin, database)
    snapshot_id = publish(client, app, admin, catalog(), "initial-catalog")
    return client, app, admin, source, snapshot_id


def test_name_edit_preserves_catalog_but_target_edit_invalidates_it(configured, database):
    client, app, admin, source, snapshot_id = configured
    assert policy(client, admin, ("biz", "orders"), []).status_code == 200
    prior = client.portal.call(binding, app, "初始管理员")
    renamed = client.patch(
        "/api/v1/admin/database-mcp/source",
        headers=admin,
        json={"row_version": source["row_version"], "name": "重命名合成数据源"},
    )
    assert renamed.status_code == 200
    value = renamed.json()["data"]
    assert value["active_snapshot_id"] == snapshot_id
    assert value["configuration_version"] == source["configuration_version"]
    assert "secret_ref" not in value
    client.portal.call(verify_binding, app, "初始管理员", prior)
    with Session(database.engine) as session:
        assert (
            session.get(DatabaseSource, int(source["id"])).secret_ref
            == "secret://business-readonly"
        )
    changed = client.patch(
        "/api/v1/admin/database-mcp/source",
        headers=admin,
        json={"row_version": value["row_version"], "host": "other-synthetic-db"},
    )
    assert changed.status_code == 200
    assert changed.json()["data"]["active_snapshot_id"] is None
    with pytest.raises(APIError):
        client.portal.call(verify_binding, app, "初始管理员", prior)
    publish(client, app, admin, catalog(), "changed-target")
    assert client.portal.call(binding, app, "初始管理员").relation_keys == set()


def test_disable_and_reenable_cannot_revive_an_old_query_binding(configured):
    client, app, admin, source, _ = configured
    assert policy(client, admin, ("biz", "orders"), []).status_code == 200
    prior = client.portal.call(binding, app, "初始管理员")
    disabled = client.patch(
        "/api/v1/admin/database-mcp/source",
        headers=admin,
        json={"row_version": source["row_version"], "status": "disabled"},
    )
    assert disabled.status_code == 200
    with pytest.raises(APIError):
        client.portal.call(verify_binding, app, "初始管理员", prior)
    enabled = client.patch(
        "/api/v1/admin/database-mcp/source",
        headers=admin,
        json={"row_version": disabled.json()["data"]["row_version"], "status": "active"},
    )
    assert enabled.status_code == 200
    assert enabled.json()["data"]["configuration_version"] > prior.source_version
    with pytest.raises(APIError):
        client.portal.call(verify_binding, app, "初始管理员", prior)


@pytest.mark.parametrize("change", ["oid", "columns", "view", "removed"])
def test_changed_external_object_never_inherits_old_grants(configured, database, change):
    client, app, admin, _, _ = configured
    name = ("biz", "safe_orders") if change == "view" else ("biz", "orders")
    department = client.post(
        "/api/v1/admin/departments",
        headers={**admin, "Idempotency-Key": "accept-department"},
        json={"name": "验收销售部"},
    ).json()["data"]["id"]
    granted = policy(client, admin, name, [department])
    assert granted.status_code == 200
    prior = client.portal.call(binding, app, "初始管理员")
    updated = []
    for relation in catalog().relations:
        if relation.key == name:
            if change == "removed":
                continue
            if change == "oid":
                relation = replace(relation, relation_oid=444)
            elif change == "columns":
                relation = replace(
                    relation, columns=(*relation.columns, SchemaColumn("note", "text", ordinal=2))
                )
            else:
                relation = replace(relation, view_definition_signature="b" * 64)
        updated.append(relation)
    updated.append(
        SchemaRelation(
            "biz", "new_object", columns=catalog().relations[0].columns, relation_oid=555
        )
    )
    publish(client, app, admin, SchemaCatalog(tuple(updated)), "changed-object")
    assert client.portal.call(binding, app, "初始管理员").relation_keys == set()
    with pytest.raises(APIError):
        client.portal.call(verify_binding, app, "初始管理员", prior)
    with Session(database.engine) as session:
        row = session.get(RelationPolicy, int(granted.json()["data"]["id"]))
        assert row.enabled is False
        assert not list(
            session.scalars(
                select(RelationDepartmentGrant).where(
                    RelationDepartmentGrant.relation_policy_id == row.id
                )
            )
        )
    if change == "removed":
        publish(client, app, admin, catalog(), "reappeared-object")
        assert client.portal.call(binding, app, "初始管理员").relation_keys == set()


def test_authorization_intersection_conflicts_and_revoked_grants(configured):
    client, app, admin, _, _ = configured
    departments = {}
    users = {}
    password = "phase-eight-synthetic-acceptance"
    for label in ("销售", "财务", "无授权"):
        created = client.post(
            "/api/v1/admin/departments",
            headers={**admin, "Idempotency-Key": f"dept-{len(departments)}"},
            json={"name": f"验收{label}部"},
        )
        assert created.status_code == 201
        departments[label] = created.json()["data"]["id"]
        created = client.post(
            "/api/v1/admin/users",
            headers={**admin, "Idempotency-Key": f"user-{len(users)}"},
            json={
                "username": f"验收{label}用户",
                "password": password,
                "role": "user",
                "department_id": departments[label],
            },
        )
        assert created.status_code == 201
        users[label] = created.json()["data"]
    granted = policy(client, admin, ("biz", "orders"), [departments["销售"]])
    assert granted.status_code == 200
    assert policy(client, admin, ("finance", "payroll"), [departments["财务"]]).status_code == 200
    assert client.portal.call(binding, app, "验收销售用户").relation_keys == {("biz", "orders")}
    assert client.portal.call(binding, app, "验收财务用户").relation_keys == {
        ("finance", "payroll")
    }
    assert client.portal.call(binding, app, "验收无授权用户").relation_keys == set()
    assert client.portal.call(binding, app, "初始管理员").relation_keys == {
        ("biz", "orders"),
        ("finance", "payroll"),
    }
    prior = client.portal.call(binding, app, "验收销售用户")
    sales_headers = auth(client, "验收销售用户", password)
    assert client.get("/api/v1/admin/database-mcp/source", headers=sales_headers).status_code == 403
    assert policy(client, sales_headers, ("biz", "safe_orders"), []).status_code == 403
    version = granted.json()["data"]["row_version"]
    invalid = policy(client, admin, ("biz", "orders"), ["9223372036854775807"], version=version)
    assert invalid.status_code == 404
    client.portal.call(verify_binding, app, "验收销售用户", prior)
    removed = policy(client, admin, ("biz", "orders"), [], version=version)
    assert removed.status_code == 200
    stale = policy(client, admin, ("biz", "orders"), [departments["财务"]], version=version)
    assert stale.status_code == 409
    restored = policy(
        client,
        admin,
        ("biz", "orders"),
        [departments["销售"]],
        version=removed.json()["data"]["row_version"],
    )
    assert restored.status_code == 200
    with pytest.raises(APIError):
        client.portal.call(verify_binding, app, "验收销售用户", prior)
    # Removing and re-adding the whitelist must not resurrect any department grant.
    removed = policy(
        client,
        admin,
        ("biz", "orders"),
        [departments["销售"]],
        enabled=False,
        version=restored.json()["data"]["row_version"],
    )
    assert removed.status_code == 200
    assert removed.json()["data"]["department_ids"] == []
    restored = policy(
        client,
        admin,
        ("biz", "orders"),
        [],
        version=removed.json()["data"]["row_version"],
    )
    assert restored.status_code == 200
    assert client.portal.call(binding, app, "验收销售用户").relation_keys == set()


def test_foreign_key_metadata_to_nonwhitelisted_object_is_not_projected(configured):
    client, app, admin, _, _ = configured
    values = catalog()
    orders = values.relations[0]
    foreign_key = SchemaConstraint(
        "orders_private_payroll_fk",
        "foreign_key",
        ("id",),
        "finance",
        "payroll",
        ("id",),
    )
    values = SchemaCatalog(
        (replace(orders, constraints=(*orders.constraints, foreign_key)), *values.relations[1:])
    )
    publish(client, app, admin, values, "foreign-key-catalog")
    assert policy(client, admin, ("biz", "orders"), []).status_code == 200
    visible = client.portal.call(binding, app, "初始管理员")
    projected = json.dumps(detail_projection(visible, [("biz", "orders")]))
    assert "orders_pk" in projected
    assert "finance" not in projected
    assert "payroll" not in projected
