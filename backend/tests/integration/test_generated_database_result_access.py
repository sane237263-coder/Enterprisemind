"""Current authorization checks for persisted database-backed results."""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid

import pytest
from test_database_admin import _create_source, _refresh
from test_ingestion import ingestion_runtime  # noqa: F401
from test_management import auth

from enterprise_mind.agent.generation_models import GeneratedFile, GenerationTask
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.database.contracts import SchemaCatalog, SchemaColumn, SchemaRelation
from enterprise_mind.database.projection import authorized_schema
from enterprise_mind.generation import service as generation_service
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.models import StorageObject

pytestmark = pytest.mark.integration

OWNER_PASSWORD = "generated-result-old-password-2026!"
NEW_PASSWORD = "generated-result-new-password-2026!"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDAT"
    b"\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99\x3d\x1d"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
MARKDOWN_BYTES = b"# Historical database report\n\nThe frozen result remains private.\n"


def test_password_reset_keeps_database_results_owner_scoped_and_currently_authorized(
    runtime, database
):
    client = runtime.client
    department_a = client.post(
        "/api/v1/admin/departments",
        headers={**runtime.admin, "Idempotency-Key": "generated-result-dept-a"},
        json={"name": "生成结果甲部门"},
    )
    assert department_a.status_code == 201, department_a.text
    department_a_id = department_a.json()["data"]["id"]
    department_b = client.post(
        "/api/v1/admin/departments",
        headers={**runtime.admin, "Idempotency-Key": "generated-result-dept-b"},
        json={"name": "生成结果乙部门"},
    )
    assert department_b.status_code == 201, department_b.text
    department_b_id = department_b.json()["data"]["id"]

    created_user = client.post(
        "/api/v1/admin/users",
        headers={**runtime.admin, "Idempotency-Key": "generated-result-owner"},
        json={
            "username": "generated-result-owner",
            "password": OWNER_PASSWORD,
            "role": "user",
            "department_id": department_a_id,
        },
    )
    assert created_user.status_code == 201, created_user.text
    owner_id = int(created_user.json()["data"]["id"])
    old_token = auth(client, "generated-result-owner", OWNER_PASSWORD)

    _create_source(client, runtime.admin, database, key="generated-result-source")
    refresh = client.post(
        "/api/v1/admin/database-mcp/schema-refresh",
        headers={**runtime.admin, "Idempotency-Key": "generated-result-refresh"},
    )
    assert refresh.status_code == 202, refresh.text
    snapshot_id = int(refresh.json()["data"]["id"])

    async def fetch_schema(_source):
        return SchemaCatalog(
            (
                SchemaRelation(
                    "biz",
                    "orders",
                    relation_oid=91234,
                    columns=(SchemaColumn("id", "bigint", ordinal=1, is_nullable=False),),
                ),
            )
        )

    refreshed = client.portal.call(_refresh, client.app, snapshot_id, fetch_schema)
    assert refreshed.status == "active"
    policy = client.put(
        "/api/v1/admin/database-mcp/relation-policies",
        headers=runtime.admin,
        json={
            "schema_name": "biz",
            "relation_name": "orders",
            "enabled": True,
            "department_ids": [department_a_id],
        },
    )
    assert policy.status_code == 200, policy.text

    async def make_source_ref() -> dict:
        async with client.app.state.sessions() as session:
            owner = await session.get(User, owner_id)
            assert owner is not None
            department = await session.get(Department, int(department_a_id))
            binding = await authorized_schema(
                session,
                CurrentIdentity(owner, department, owner.credential_version),
            )
            return {
                "source_type": "database",
                "database_source_id": str(binding.database_source_id),
                "schema_snapshot_id": str(binding.schema_snapshot_id),
                "source_version": binding.source_version,
                "permission_fingerprint": binding.permission_fingerprint,
                "referenced_relations": [
                    relation.quoted_qualified_name for relation in binding.catalog.relations
                ],
            }

    source_ref = client.portal.call(make_source_ref)
    assert source_ref["referenced_relations"] == ['"biz"."orders"']
    # This persisted file models an older source_refs shape where the
    # fingerprint field was absent. Optional legacy fields remain supported.
    legacy_source_ref = {
        key: value for key, value in source_ref.items() if key != "permission_fingerprint"
    }
    base = client.post(
        "/api/v1/knowledge-bases",
        headers={**runtime.admin, "Idempotency-Key": "generated-result-target-base"},
        json={
            "name": "生成结果导入库",
            "type": "department",
            "department_id": department_a_id,
        },
    )
    assert base.status_code == 201, base.text

    async def save_draft() -> int:
        async with client.app.state.sessions() as session, session.begin():
            owner = await session.get(User, owner_id)
            assert owner is not None
            department = await session.get(Department, int(department_a_id))
            draft = await generation_service.create_draft(
                session,
                CurrentIdentity(owner, department, owner.credential_version),
                title="历史数据库草稿",
                body_markdown="# 历史结果\n\n可按当前来源授权访问。",
                source_refs=[source_ref],
            )
            return draft.id

    draft_id = client.portal.call(save_draft)

    async def submit_render_task(idempotency_key: str, file_name: str, revision: int) -> dict:
        async with client.app.state.sessions() as session, session.begin():
            owner = await session.get(User, owner_id)
            assert owner is not None
            department = await session.get(Department, int(department_a_id))
            draft = await generation_service.get_draft(
                session,
                CurrentIdentity(owner, department, owner.credential_version),
                draft_id,
                current_access_only=True,
            )
            task = await generation_service.create_render_task(
                session,
                CurrentIdentity(owner, department, owner.credential_version),
                draft_id=draft_id,
                expected_revision=revision,
                file_format="md",
                file_name=file_name,
                idempotency_key=idempotency_key,
            )
            return {
                "id": task.id,
                "request_fingerprint": task.request_fingerprint,
                "source_refs": [dict(ref) for ref in task.source_refs or []],
                "input_ref": dict(task.input_ref or {}),
                "draft_source_refs": [dict(ref) for ref in draft.source_refs or []],
            }

    # Keep the pre-reset queued task unconsumed: this exercises persistence and
    # idempotent submission only, while the worker remains independently strict.
    pre_reset_render = client.portal.call(
        submit_render_task, "generated-result-render-before-reset", "before-reset.md", 1
    )
    assert pre_reset_render["source_refs"] == [source_ref]

    async def save_result(
        file_name: str,
        file_format: str,
        generation_kind: str,
        content: bytes,
        mime: str,
        refs,
    ) -> tuple[int, int]:
        digest = hashlib.sha256(content).hexdigest()
        key = runtime.worker.storage.new_key("generated")
        await asyncio.to_thread(runtime.worker.storage.upload, key, io.BytesIO(content), mime)
        async with client.app.state.sessions() as session, session.begin():
            owner = await session.get(User, owner_id)
            assert owner is not None
            storage_object = StorageObject(
                bucket_name=runtime.worker.storage.bucket,
                object_key=key,
                object_type="generated",
                size_bytes=len(content),
                content_type=mime,
                content_sha256=digest,
                status="available",
            )
            session.add(storage_object)
            await session.flush()
            task = GenerationTask(
                owner_user_id=owner.id,
                kind=generation_kind,
                format=file_format,
                idempotency_key=uuid.uuid4().hex,
                request_fingerprint=digest,
                input_ref={"title": file_name},
                source_refs=[refs],
                status="succeeded",
                file_id=storage_object.id,
            )
            session.add(task)
            await session.flush()
            generated_file = GeneratedFile(
                task_id=task.id,
                owner_user_id=owner.id,
                storage_object_id=storage_object.id,
                file_name=file_name,
                file_format=file_format,
                generation_kind=generation_kind,
                source_refs=[refs],
                content_sha256=digest,
                size_bytes=len(content),
            )
            session.add(generated_file)
            await session.flush()
            return generated_file.id, task.id

    chart_id, chart_task_id = client.portal.call(
        save_result,
        "historical-orders-chart.png",
        "png",
        "chart_render",
        PNG_BYTES,
        "image/png",
        source_ref,
    )
    file_id, _ = client.portal.call(
        save_result,
        "historical-orders-report.md",
        "md",
        "content_render",
        MARKDOWN_BYTES,
        "text/markdown",
        legacy_source_ref,
    )

    assert (
        client.get(f"/api/v1/generated-files/{chart_id}/preview", headers=old_token).status_code
        == 200
    )
    assert (
        client.get(f"/api/v1/generated-files/{file_id}/download", headers=old_token).content
        == MARKDOWN_BYTES
    )
    not_owner = client.get(f"/api/v1/generated-files/{chart_id}/preview", headers=runtime.admin)
    assert not_owner.status_code == 404

    reset = client.post(
        f"/api/v1/admin/users/{owner_id}/password-reset",
        headers=runtime.admin,
        json={"row_version": 1, "new_password": NEW_PASSWORD},
    )
    assert reset.status_code == 200, reset.text
    old_token_response = client.get("/api/v1/auth/me", headers=old_token)
    assert old_token_response.status_code == 401, old_token_response.text
    assert old_token_response.json()["error"]["code"] == ErrorCode.AUTH_REQUIRED.value
    new_token = auth(client, "generated-result-owner", NEW_PASSWORD)

    async def assert_worker_binding_stays_strict() -> None:
        async with client.app.state.sessions() as session:
            owner = await session.get(User, owner_id)
            assert owner is not None
            department = await session.get(Department, int(department_a_id))
            with pytest.raises(APIError) as error:
                await generation_service.revalidate_generated_sources(
                    session,
                    CurrentIdentity(owner, department, owner.credential_version),
                    [source_ref],
                )
            assert error.value.code == ErrorCode.PERMISSION_DENIED

    client.portal.call(assert_worker_binding_stays_strict)

    current_source_ref = client.portal.call(make_source_ref)
    assert current_source_ref["permission_fingerprint"] != source_ref["permission_fingerprint"]
    replayed_old_key = client.portal.call(
        submit_render_task, "generated-result-render-before-reset", "before-reset.md", 1
    )
    assert replayed_old_key["id"] == pre_reset_render["id"]
    assert replayed_old_key["request_fingerprint"] == pre_reset_render["request_fingerprint"]

    post_reset_render = client.portal.call(
        submit_render_task, "generated-result-render-after-reset", "after-reset.md", 1
    )
    assert post_reset_render["source_refs"] == post_reset_render["input_ref"]["source_refs"]
    assert (
        post_reset_render["source_refs"][0]["permission_fingerprint"]
        == current_source_ref["permission_fingerprint"]
    )
    assert post_reset_render["draft_source_refs"] == [source_ref]
    assert post_reset_render["input_ref"]["content_hash"] == generation_service.draft_hash(
        post_reset_render["input_ref"]["title"],
        post_reset_render["input_ref"]["body_markdown"],
        post_reset_render["input_ref"]["tables"],
        post_reset_render["input_ref"]["source_refs"],
    )
    replayed_new_key = client.portal.call(
        submit_render_task, "generated-result-render-after-reset", "after-reset.md", 1
    )
    assert replayed_new_key["id"] == post_reset_render["id"]
    assert replayed_new_key["request_fingerprint"] == post_reset_render["request_fingerprint"]
    with pytest.raises(APIError) as conflicting_render:
        client.portal.call(
            submit_render_task,
            "generated-result-render-after-reset",
            "different-request.md",
            1,
        )
    assert conflicting_render.value.code == ErrorCode.IDEMPOTENCY_KEY_CONFLICT

    chart = client.get(f"/api/v1/generated-files/{chart_id}/preview", headers=new_token)
    assert chart.status_code == 200, chart.text
    assert chart.content == PNG_BYTES
    report = client.get(f"/api/v1/generated-files/{file_id}/download", headers=new_token)
    assert report.status_code == 200, report.text
    assert report.content == MARKDOWN_BYTES
    draft = client.get(f"/api/v1/drafts/{draft_id}", headers=new_token)
    assert draft.status_code == 200, draft.text
    edited_draft = client.patch(
        f"/api/v1/drafts/{draft_id}",
        headers=new_token,
        json={"expected_revision": 1, "title": "重置后仍可编辑"},
    )
    assert edited_draft.status_code == 200, edited_draft.text
    task = client.get(f"/api/v1/generation-tasks/{chart_task_id}", headers=new_token)
    assert task.status_code == 200, task.text
    cancelled = client.post(f"/api/v1/generation-tasks/{chart_task_id}/cancel", headers=new_token)
    assert cancelled.status_code == 200, cancelled.text
    imported = client.post(
        f"/api/v1/generated-files/{file_id}/import-previews",
        headers={**new_token, "Idempotency-Key": "generated-result-import-after-reset"},
        json={"knowledge_base_id": base.json()["data"]["id"], "name": "导入后重置.md"},
    )
    assert imported.status_code == 202, imported.text

    async def reject_malformed_or_stale_source_refs() -> None:
        async with client.app.state.sessions() as session:
            owner = await session.get(User, owner_id)
            assert owner is not None
            department = await session.get(Department, int(department_a_id))
            identity = CurrentIdentity(owner, department, owner.credential_version)
            invalid_refs = (
                {**source_ref, "referenced_relations": []},
                {**source_ref, "referenced_relations": ['"biz"."payroll"']},
                {**source_ref, "referenced_relations": [17]},
                {**source_ref, "source_version": source_ref["source_version"] + 1},
            )
            for ref in invalid_refs:
                with pytest.raises(APIError) as error:
                    await generation_service.revalidate_generated_sources(
                        session, identity, [ref], current_access_only=True
                    )
                assert error.value.code == ErrorCode.PERMISSION_DENIED

    client.portal.call(reject_malformed_or_stale_source_refs)

    revoked = client.put(
        "/api/v1/admin/database-mcp/relation-policies",
        headers=runtime.admin,
        json={
            "schema_name": "biz",
            "relation_name": "orders",
            "enabled": False,
            "department_ids": [],
            "row_version": 1,
        },
    )
    assert revoked.status_code == 200, revoked.text
    denied_during_revocation = client.get(
        f"/api/v1/generated-files/{chart_id}/preview", headers=new_token
    )
    assert denied_during_revocation.status_code == 403, denied_during_revocation.text

    async def reject_render_while_revoked() -> ErrorCode:
        async with client.app.state.sessions() as session, session.begin():
            owner = await session.get(User, owner_id)
            assert owner is not None
            department = await session.get(Department, int(department_a_id))
            with pytest.raises(APIError) as error:
                await generation_service.create_render_task(
                    session,
                    CurrentIdentity(owner, department, owner.credential_version),
                    draft_id=draft_id,
                    expected_revision=2,
                    file_format="md",
                    file_name="revoked.md",
                    idempotency_key="generated-result-render-while-revoked",
                )
            return error.value.code

    assert client.portal.call(reject_render_while_revoked) == ErrorCode.PERMISSION_DENIED

    # Persistent results follow current authorization: a restored grant makes
    # the old result readable again. In-flight query bindings still use the
    # unchanged strict permission-fingerprint checks.
    restored = client.put(
        "/api/v1/admin/database-mcp/relation-policies",
        headers=runtime.admin,
        json={
            "schema_name": "biz",
            "relation_name": "orders",
            "enabled": True,
            "department_ids": [department_a_id],
            "row_version": 2,
        },
    )
    assert restored.status_code == 200, restored.text
    assert (
        client.get(f"/api/v1/generated-files/{chart_id}/preview", headers=new_token).status_code
        == 200
    )

    moved = client.patch(
        f"/api/v1/admin/users/{owner_id}",
        headers=runtime.admin,
        json={"row_version": 2, "department_id": department_b_id},
    )
    assert moved.status_code == 200, moved.text
    denied_after_department_change = client.get(
        f"/api/v1/generated-files/{chart_id}/preview", headers=new_token
    )
    assert denied_after_department_change.status_code == 403, denied_after_department_change.text
