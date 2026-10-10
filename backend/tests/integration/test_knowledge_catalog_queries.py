from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from test_management import client_for, created, setup_organization

from enterprise_mind.agent.knowledge_catalog import CatalogInput, read_catalog
from enterprise_mind.identity.service import authenticate
from enterprise_mind.knowledge.models import DeletionBatch, KnowledgeBase

pytestmark = pytest.mark.integration
K = "/api/v1/knowledge-bases"


def read_for(client, authorization, body):
    token = authorization["Authorization"].removeprefix("Bearer ")

    async def run():
        async with client.app.state.sessions() as session, session.begin():
            identity = await authenticate(session, token, client.app.state.tokens)
            return await read_catalog(session, identity, body)

    return client.portal.call(run)


def mark_recycled(database, base):
    deleted_at = datetime.now(UTC)
    purge_after = deleted_at + timedelta(days=30)
    with Session(database.engine) as session, session.begin():
        row = session.get(KnowledgeBase, int(base["id"]))
        assert row is not None
        batch = DeletionBatch(
            root_type="knowledge_base",
            root_id=row.id,
            root_name=row.name,
            deleted_at=deleted_at,
            purge_after=purge_after,
        )
        session.add(batch)
        session.flush()
        row.deleted_at = deleted_at
        row.purge_after = purge_after
        row.deletion_batch_id = batch.id


def test_catalog_matches_active_api_authorization_and_hides_recycled_bases(database):
    with client_for(database) as (client, admin):
        department_a, department_b, _, ordinary = setup_organization(client, admin)
        public = created(client, admin, K, {"name": "企业公共资料", "type": "public"})
        own = created(
            client,
            admin,
            K,
            {
                "name": "本部门资料",
                "type": "department",
                "department_id": department_a["id"],
            },
        )
        other = created(
            client,
            admin,
            K,
            {
                "name": "其他部门资料",
                "type": "department",
                "department_id": department_b["id"],
            },
        )
        recycled = created(client, admin, K, {"name": "已回收资料", "type": "public"})
        mark_recycled(database, recycled)

        api_items = client.get(K, headers=ordinary).json()["data"]
        actual = read_for(client, ordinary, CatalogInput())
        admin_actual = read_for(client, admin, CatalogInput())

        assert [item["id"] for item in actual["items"]] == [
            public["id"],
            own["id"],
        ]
        assert [item["id"] for item in actual["items"]] == [item["id"] for item in api_items]
        assert actual["has_more"] is False and actual["next_after_id"] is None
        assert all(set(item) == {"id", "name", "type"} for item in actual["items"])
        assert other["id"] not in {item["id"] for item in actual["items"]}
        assert recycled["id"] not in {
            item["id"] for item in actual["items"] + admin_actual["items"]
        }
        assert {item["id"] for item in admin_actual["items"]} == {
            public["id"],
            own["id"],
            other["id"],
        }


def test_catalog_keyset_pages_continue_without_skips_or_false_completion(database):
    with client_for(database) as (client, admin):
        bases = [
            created(client, admin, K, {"name": f"分页知识库 {index}", "type": "public"})
            for index in range(3)
        ]

        first = read_for(client, admin, CatalogInput(limit=2))
        second = read_for(
            client,
            admin,
            CatalogInput(limit=2, after_id=first["next_after_id"]),
        )

        assert [item["id"] for item in first["items"]] == [item["id"] for item in bases[:2]]
        assert first["count"] == 2 and first["has_more"] is True
        assert first["next_after_id"] == int(bases[1]["id"])
        assert [item["id"] for item in second["items"]] == [bases[2]["id"]]
        assert second["count"] == 1 and second["has_more"] is False
        assert second["next_after_id"] is None


def test_catalog_uses_identity_department_at_read_time(database):
    with client_for(database) as (client, admin):
        department_a, department_b, user, ordinary = setup_organization(client, admin)
        public = created(client, admin, K, {"name": "跨部门公共", "type": "public"})
        old_department = created(
            client,
            admin,
            K,
            {
                "name": "原部门库",
                "type": "department",
                "department_id": department_a["id"],
            },
        )
        new_department = created(
            client,
            admin,
            K,
            {
                "name": "新部门库",
                "type": "department",
                "department_id": department_b["id"],
            },
        )
        initial = read_for(client, ordinary, CatalogInput())
        assert {item["id"] for item in initial["items"]} == {
            public["id"],
            old_department["id"],
        }

        response = client.patch(
            f"/api/v1/admin/users/{user['id']}",
            headers=admin,
            json={"row_version": 1, "department_id": department_b["id"]},
        )
        assert response.status_code == 200, response.text
        after_transfer = read_for(client, ordinary, CatalogInput())

        assert {item["id"] for item in after_transfer["items"]} == {
            public["id"],
            new_department["id"],
        }
        assert old_department["id"] not in {item["id"] for item in after_transfer["items"]}
