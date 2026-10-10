import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_agent import Model, post, snapshot
from test_agent_lifecycle import WriteModel, pending, respond
from test_management import auth
from test_rag_pipeline import indexed, ingestion_runtime, retrieve  # noqa: F401

from enterprise_mind.agent.checkpoints import initialize_checkpoints
from enterprise_mind.agent.models import Conversation, Message, ToolCall
from enterprise_mind.agent.runner import Execution
from enterprise_mind.agent.schemas import Plan
from enterprise_mind.app import create_app
from enterprise_mind.identity.models import Department, User
from enterprise_mind.ingestion.cleanup import CleanupWorker, schedule_cleanup
from enterprise_mind.ingestion.models import (
    Document,
    OutboxEvent,
    ProcessingTask,
    Revision,
    StorageObject,
)
from enterprise_mind.knowledge.models import KnowledgeBase
from enterprise_mind.knowledge.pending_models import PendingAction

pytestmark = pytest.mark.integration


@pytest.fixture
def file_agent(database, ingestion_settings):
    database.initialize()
    app = create_app(database.settings, ingestion_settings[0])
    app.state.agent.start = lambda: None
    app.state.agent.model = Model()
    with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}) as client:
        client.portal.call(initialize_checkpoints, database.url)
        yield client, auth(client, "初始管理员", database.admin_password), app.state.agent


def setup(active, name="资料"):
    client, headers, _ = active
    base = post(client, headers, "/api/v1/knowledge-bases", {"name": name, "type": "public"})
    assert base.status_code == 201, base.text
    return base.json()["data"]["id"], new_conversation(active)


def new_conversation(active):
    client, headers, _ = active
    result = post(client, headers, "/api/v1/conversations", {"title": "附件会话"})
    assert result.status_code == 201, result.text
    return result.json()["data"]["id"]


def stage(active, conversation_id, *, key=None, files=None, headers=None):
    client, identity, _ = active
    return client.post(
        f"/api/v1/conversations/{conversation_id}/attachments",
        headers={**(headers or identity), "Idempotency-Key": key or uuid.uuid4().hex},
        files=files or [("files", ("附件.txt", b"PRIVATE_ATTACHMENT_BODY", "text/plain"))],
    )


def staged(response):
    assert response.status_code == 201, response.text
    item = response.json()["data"]["items"][0]
    assert item["status"] == "accepted", response.text
    return item["staged_upload_id"]


def submit(active, conversation_id, identifiers=(), key=None):
    client, headers, _ = active
    return post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": "请按要求保存附件。", "staged_upload_ids": list(identifiers)},
        key,
    )


def execute_upload(active, base, conversation_id, identifier, *, display_name=None):
    client, _, manager = active
    file = {"staged_upload_id": identifier}
    if display_name:
        file["name"] = display_name
    manager.model = WriteModel("document_upload", {"knowledge_base_id": base, "files": [file]})
    response = submit(active, conversation_id, [identifier])
    assert response.status_code == 202, response.text
    run_id = int(response.json()["data"]["id"])
    assert client.portal.call(manager.execute, run_id)
    assert snapshot(active, run_id)["status"] == "completed", snapshot(active, run_id)
    return run_id


def test_staging_validation_replay_concurrency_and_message_binding(file_agent, database):
    client, headers, _ = file_agent
    conversation_id = new_conversation(file_agent)
    files = [
        ("files", ("valid.txt", b"hello", "text/plain")),
        ("files", ("bad.exe", b"MZbinary", "application/octet-stream")),
        ("files", ("fake.pdf", b"not pdf", "application/pdf")),
    ]
    first = stage(file_agent, conversation_id, files=files, key="stage-once")
    assert first.status_code == 201, first.text
    assert [i["status"] for i in first.json()["data"]["items"]] == [
        "accepted",
        "rejected",
        "rejected",
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: stage(file_agent, conversation_id, files=files, key="stage-once"),
                range(2),
            )
        )
    assert all(r.json()["data"] == first.json()["data"] for r in results)
    assert stage(file_agent, conversation_id, key="stage-once").status_code == 409
    identifier = first.json()["data"]["items"][0]["staged_upload_id"]
    message = submit(file_agent, conversation_id, [identifier], "bound-message")
    assert message.status_code == 202, message.text
    assert (
        submit(file_agent, conversation_id, [identifier], "bound-message").json()["data"]
        == message.json()["data"]
    )
    listing = client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers)
    assert listing.json()["data"][0]["content_parts"][0]["available"]
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(StorageObject)) == 1
        assert session.scalar(select(func.count()).select_from(Document)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert session.scalars(select(Message)).one().content_parts == [
            {"type": "attachment", "staged_upload_id": identifier}
        ]
    for forbidden in ("object_key", "bucket", "sha256", "hello"):
        assert forbidden not in first.text + listing.text


def test_attachment_upload_creates_one_original_task_and_safe_model_catalog(file_agent, database):
    client, headers, manager = file_agent
    base, conversation_id = setup(file_agent)
    identifier = staged(stage(file_agent, conversation_id))
    observed = []
    original = WriteModel.plan

    async def capture(self, messages, tools, feedback):
        observed.extend(str(m.content) for m in messages)
        return await original(self, messages, tools, feedback)

    model = WriteModel(
        "document_upload",
        {
            "knowledge_base_id": base,
            "files": [{"staged_upload_id": identifier, "name": "已保存.txt"}],
        },
    )
    model.plan = capture.__get__(model, WriteModel)
    manager.model = model
    response = submit(file_agent, conversation_id, [identifier])
    assert response.status_code == 202, response.text
    run_id = int(response.json()["data"]["id"])
    assert client.portal.call(manager.execute, run_id)
    assert snapshot(file_agent, run_id)["status"] == "completed", snapshot(file_agent, run_id)
    assert not client.portal.call(manager.execute, run_id)
    assert any('"staged_upload_id": "' + identifier + '"' in m for m in observed)
    with Session(database.engine) as session:
        obj = session.scalars(select(StorageObject)).one()
        doc = session.scalars(select(Document)).one()
        assert doc.name == "已保存.txt" and doc.processing_status == "pending"
        assert obj.status == "available" and obj.retention_until is None
        assert session.scalars(select(Revision)).one().original_object_id == obj.id
        assert session.scalar(select(func.count()).select_from(ProcessingTask)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        call = session.scalars(select(ToolCall)).one()
        assert call.status == "success" and call.risk_level == "low"
        assert call.result_summary["items"][0]["document_id"] == str(doc.id)
        assert any("已保存.txt" in result for result in model.results)
        for forbidden in (obj.object_key, obj.bucket_name, "PRIVATE_ATTACHMENT_BODY"):
            assert forbidden not in str(observed) + str(call.arguments) + str(model.results)
    history = client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    ).json()["data"]
    assert history[0]["content_parts"][0]["available"] is False
    consumed = submit(file_agent, conversation_id, [identifier])
    assert (
        consumed.status_code == 409 and consumed.json()["error"]["code"] == "ATTACHMENT_UNAVAILABLE"
    )


@pytest.mark.parametrize(
    "violation", ["other_owner", "other_conversation", "unbound", "expired", "forged"]
)
def test_file_gateway_rejects_unowned_unbound_and_expired_ids(file_agent, database, violation):
    client, headers, manager = file_agent
    base, first = setup(file_agent)
    identifier = staged(stage(file_agent, first))
    target = first
    attach = []
    if violation == "other_owner":
        with Session(database.engine) as session, session.begin():
            admin = session.scalars(select(User)).one()
            other = User(
                username="other",
                username_key="other",
                password_hash=admin.password_hash,
                role="admin",
            )
            session.add(other)
        headers = auth(client, "other", database.admin_password)
        assert stage(file_agent, first, headers=headers).status_code == 404
        assert (
            client.get(f"/api/v1/conversations/{first}/attachments", headers=headers).status_code
            == 404
        )
        active = (client, headers, manager)
        target = new_conversation(active)
    else:
        active = file_agent
    if violation == "other_conversation":
        target = new_conversation(file_agent)
        assert submit(file_agent, target, [identifier]).status_code == 404
    if violation == "expired":
        attach = [identifier]
    if violation == "forged":
        identifier = "999999"
    response = submit(active, target, attach)
    assert response.status_code == 202, response.text
    if violation == "expired":
        with Session(database.engine) as session, session.begin():
            session.get(StorageObject, int(identifier)).retention_until = datetime.now(
                UTC
            ) - timedelta(seconds=1)
    manager.model = WriteModel(
        "document_upload", {"knowledge_base_id": base, "files": [{"staged_upload_id": identifier}]}
    )
    run_id = int(response.json()["data"]["id"])
    client.portal.call(manager.execute, run_id)
    state = snapshot(active, run_id)
    assert state["status"] == "failed", state
    assert state["failure_code"] in {"RESOURCE_NOT_FOUND", "ATTACHMENT_UNAVAILABLE"}
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_name_conflict_keeps_attachment_for_followup_rename(file_agent, database):
    client, _, manager = file_agent
    base, conversation_id = setup(file_agent)
    first = staged(stage(file_agent, conversation_id))
    execute_upload(file_agent, base, conversation_id, first)
    second = staged(stage(file_agent, conversation_id))
    run_id = execute_upload(file_agent, base, conversation_id, second)
    with Session(database.engine) as session:
        call = session.scalars(select(ToolCall).where(ToolCall.agent_run_id == run_id)).one()
        assert call.result_summary["items"][0]["status"] == "name_conflict"
        assert session.get(StorageObject, int(second)).status == "staged"
        assert session.scalar(select(func.count()).select_from(Document)) == 1
    # Follow-up may use an attachment bound in an earlier message without resending bytes.
    manager.model = WriteModel(
        "document_upload",
        {"knowledge_base_id": base, "files": [{"staged_upload_id": second, "name": "另存.txt"}]},
    )
    response = submit(file_agent, conversation_id)
    run_id = int(response.json()["data"]["id"])
    client.portal.call(manager.execute, run_id)
    assert snapshot(file_agent, run_id)["status"] == "completed", snapshot(file_agent, run_id)
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 2
        assert session.get(StorageObject, int(second)).status == "available"


def setup_overwrite(active, database):
    base, conversation_id = setup(active)
    identifier = staged(stage(active, conversation_id))
    execute_upload(active, base, conversation_id, identifier)
    with Session(database.engine) as session, session.begin():
        doc = session.scalars(select(Document)).one()
        revision = session.scalars(select(Revision)).one()
        task = session.scalars(select(ProcessingTask)).one()
        # Prepare a terminal original to test overwrite's active-generation preservation.
        task.status, task.finished_at = "failed", datetime.now(UTC)
        revision.processing_status = doc.processing_status = "failed"
        document_id = str(doc.id)
    replacement = staged(
        stage(
            active,
            conversation_id,
            files=[("files", ("replacement.txt", b"NEW_PRIVATE_BODY", "text/plain"))],
        )
    )
    client, _, manager = active
    manager.model = WriteModel(
        "document_overwrite", {"target": {"id": document_id}, "staged_upload_id": replacement}
    )
    response = submit(active, conversation_id, [replacement])
    assert response.status_code == 202, response.text
    run_id = int(response.json()["data"]["id"])
    client.portal.call(manager.execute, run_id)
    assert snapshot(active, run_id)["status"] == "waiting_confirmation", snapshot(active, run_id)
    return conversation_id, document_id, replacement, run_id, pending(database, run_id)[0]


@pytest.mark.parametrize("decision", ["accept", "reject", "expire", "change_arguments", "stop"])
def test_overwrite_requires_bound_confirmation_and_handles_rejection(
    file_agent, database, decision
):
    client, headers, manager = file_agent
    _, document_id, replacement, run_id, action_id = setup_overwrite(file_agent, database)
    with Session(database.engine) as session:
        assert session.get(Document, int(document_id)).latest_revision_no == 1
        action = session.get(PendingAction, action_id)
        assert (
            action.operation_name == "document.overwrite"
            and "replacement.txt" in action.prompt_text
        )
        assert "NEW_PRIVATE_BODY" not in action.prompt_text
    if decision == "stop":
        assert client.post(f"/api/v1/agent-runs/{run_id}/stop", headers=headers).status_code == 200
        assert (
            client.post(f"/api/v1/agent-runs/{run_id}/resume", headers=headers).status_code == 202
        )
        client.portal.call(manager.execute, run_id)
        new_id, _ = pending(database, run_id)
        assert new_id != action_id
        action_id = new_id
    assert (
        respond(file_agent, action_id, "reject" if decision == "reject" else "accept").status_code
        == 200
    )
    with Session(database.engine) as session, session.begin():
        assert session.get(Document, int(document_id)).latest_revision_no == 1
        if decision == "expire":
            session.get(StorageObject, int(replacement)).retention_until = datetime.now(
                UTC
            ) - timedelta(seconds=1)
        elif decision == "change_arguments":
            call = session.scalars(select(ToolCall).where(ToolCall.agent_run_id == run_id)).one()
            call.arguments = {**call.arguments, "file_name": "changed.txt"}
    client.portal.call(manager.execute, run_id)
    state = snapshot(file_agent, run_id)
    assert state["status"] == (
        "failed" if decision in {"expire", "change_arguments"} else "completed"
    ), state
    with Session(database.engine) as session:
        assert session.get(Document, int(document_id)).latest_revision_no == (
            2 if decision in {"accept", "stop"} else 1
        )
        if decision == "reject":
            assert session.get(StorageObject, int(replacement)).retention_until <= datetime.now(UTC)


def test_expiry_and_conversation_delete_clean_staged_only(file_agent, database, monkeypatch):
    client, headers, _ = file_agent
    base, conversation_id = setup(file_agent)
    kept = staged(stage(file_agent, conversation_id))
    execute_upload(file_agent, base, conversation_id, kept)
    expired = staged(stage(file_agent, conversation_id))
    pending_file = staged(stage(file_agent, conversation_id))
    with Session(database.engine) as session, session.begin():
        session.get(StorageObject, int(expired)).retention_until = datetime.now(UTC) - timedelta(
            seconds=1
        )
    client.portal.call(schedule_cleanup, client.app.state.sessions, 60, 100)
    current = client.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()["data"]
    action = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/deletion-requests",
        {"row_version": current["row_version"]},
    )
    assert action.status_code == 200, action.text
    assert respond(file_agent, action.json()["data"]["id"]).status_code == 200
    cleanup = CleanupWorker(client.app.state.sessions, client.app.state.storage)
    with Session(database.engine) as session:
        assert session.get(Conversation, int(conversation_id)) is None
        assert session.get(StorageObject, int(kept)).status == "available"
        assert session.get(StorageObject, int(pending_file)).status == "pending_delete"
        events = list(
            session.scalars(
                select(OutboxEvent.id).where(OutboxEvent.event_type == "storage.delete.requested")
            )
        )
    assert len(events) == 2
    for event in events:
        assert client.portal.call(cleanup.handle, event)
    with Session(database.engine) as session:
        original = session.get(StorageObject, int(kept))
        client.app.state.storage.verify(
            original.object_key, original.size_bytes, original.content_sha256
        )
        assert session.get(StorageObject, int(expired)) is None
        assert session.get(StorageObject, int(pending_file)) is None


def test_file_commit_before_checkpoint_recovers_without_duplicate_document(
    file_agent, database, monkeypatch
):
    client, _, manager = file_agent
    base, conversation_id = setup(file_agent)
    identifier = staged(stage(file_agent, conversation_id))
    manager.model = WriteModel(
        "document_upload", {"knowledge_base_id": base, "files": [{"staged_upload_id": identifier}]}
    )
    response = submit(file_agent, conversation_id, [identifier])
    run_id = int(response.json()["data"]["id"])
    original = Execution.execute_mutation

    async def crash():
        reached = asyncio.Event()

        async def pause(self, state):
            result = await original(self, state)
            reached.set()
            await asyncio.Event().wait()
            return result

        monkeypatch.setattr(Execution, "execute_mutation", pause)
        task = asyncio.create_task(manager.execute(run_id))
        await asyncio.wait_for(reached.wait(), 8)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    client.portal.call(crash)
    monkeypatch.setattr(Execution, "execute_mutation", original)
    assert client.portal.call(manager.execute, run_id)
    assert snapshot(file_agent, run_id)["status"] == "completed", snapshot(file_agent, run_id)
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 1
        assert session.scalar(select(func.count()).select_from(ProcessingTask)) == 1
        assert session.scalar(select(func.count()).select_from(StorageObject)) == 1


def test_attachment_to_rag_and_failed_overwrite_preserves_active(file_agent, indexed, database):  # noqa: F811
    runtime, retrieval, _, embedding, _ = indexed
    runtime.run(runtime.client.app.state.agent.close)
    client, _, manager = file_agent
    base, conversation_id = setup(file_agent)
    identifier = staged(
        stage(
            file_agent,
            conversation_id,
            files=[("files", ("manual.txt", "ABC-123 原有效资料。".encode(), "text/plain"))],
        )
    )
    execute_upload(file_agent, base, conversation_id, identifier)
    with Session(database.engine) as session:
        event_id = session.scalars(select(OutboxEvent.id)).one()
        doc_id = session.scalars(select(Document.id)).one()
    assert runtime.run(runtime.worker.handle_event, event_id)
    assert retrieve(indexed).contexts
    with Session(database.engine) as session:
        doc = session.get(Document, doc_id)
        assert doc.processing_status == "success"
        previous_active = doc.active_revision_id
    replacement = staged(
        stage(
            file_agent,
            conversation_id,
            files=[("files", ("next.txt", "ABC-123 新替换资料。".encode(), "text/plain"))],
        )
    )
    manager.model = WriteModel(
        "document_overwrite",
        {
            "target": {"id": str(doc_id)},
            "staged_upload_id": replacement,
        },
    )
    response = submit(file_agent, conversation_id, [replacement])
    run_id = int(response.json()["data"]["id"])
    client.portal.call(manager.execute, run_id)
    assert snapshot(file_agent, run_id)["status"] == "waiting_confirmation"
    action_id, _ = pending(database, run_id)
    assert respond(file_agent, action_id).status_code == 200
    client.portal.call(manager.execute, run_id)
    assert snapshot(file_agent, run_id)["status"] == "completed"
    with Session(database.engine) as session:
        call = session.scalars(select(ToolCall).where(ToolCall.agent_run_id == run_id)).one()
        task_id = call.result_summary["processing_task_id"]
        event_id = session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.deduplication_key == f"document.processing:{task_id}"
            )
        )
        assert session.get(Document, doc_id).active_revision_id == previous_active
    embedding.fail = True
    assert runtime.run(runtime.worker.handle_event, event_id)
    with Session(database.engine) as session:
        doc = session.get(Document, doc_id)
        assert doc.processing_status == "failed"
        assert doc.active_revision_id == previous_active
    embedding.fail = False
    result = retrieve(indexed)
    assert result.contexts
    assert all(
        c.revision_id == previous_active for block in result.contexts for c in block.candidates
    )


def test_attachment_only_message_requests_context_without_premature_upload(file_agent, database):
    client, headers, manager = file_agent
    conversation_id = new_conversation(file_agent)
    identifier = staged(stage(file_agent, conversation_id))

    class ClarifyingModel(Model):
        async def plan(self, messages, tools, feedback):
            assert any("尚未说明要执行的操作" in str(m.content) for m in messages)
            return Plan(
                intent_kind="clarify",
                requires_enterprise_facts=False,
                clarification="请说明附件要保存到哪个知识库。",
            )

    manager.model = ClarifyingModel()
    response = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"staged_upload_ids": [identifier]},
    )
    assert response.status_code == 202, response.text
    run_id = int(response.json()["data"]["id"])
    client.portal.call(manager.execute, run_id)
    assert snapshot(file_agent, run_id)["status"] == "completed", snapshot(file_agent, run_id)
    with Session(database.engine) as session:
        assert "请说明" in session.scalar(
            select(Message.content_text).where(Message.role == "assistant")
        )
        assert session.get(StorageObject, int(identifier)).status == "staged"
        assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_stage_revalidates_identity_after_transfer_and_storage_failure_allows_retry(
    file_agent, database, monkeypatch
):
    from enterprise_mind.api import attachments as endpoint
    from enterprise_mind.api.errors import APIError, ErrorCode

    client, _, _ = file_agent
    conversation_id = new_conversation(file_agent)
    storage = client.app.state.storage
    original_verify = storage.verify

    def fail(*args):
        raise APIError(ErrorCode.STORAGE_UNAVAILABLE)

    monkeypatch.setattr(storage, "verify", fail)
    assert stage(file_agent, conversation_id, key="recover-upload").status_code == 503
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(StorageObject)) == 0
    monkeypatch.setattr(storage, "verify", original_verify)
    assert staged(stage(file_agent, conversation_id, key="recover-upload"))
    original_receive = endpoint.receive_upload

    async def revoke_after_receive(*args, **kwargs):
        result = await original_receive(*args, **kwargs)
        with Session(database.engine) as session, session.begin():
            session.scalars(select(User)).one().credential_version += 1
        return result

    monkeypatch.setattr(endpoint, "receive_upload", revoke_after_receive)
    assert stage(file_agent, conversation_id).status_code == 401
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(StorageObject)) == 1


def test_batch_upload_partial_conflict_and_scope_revocation_before_execution(
    file_agent, database, monkeypatch
):
    client, _, manager = file_agent
    base, _ = setup(file_agent)
    with Session(database.engine) as session, session.begin():
        admin = session.scalars(select(User)).one()
        dept = Department(name="研发", name_key="研发")
        other = Department(name="财务", name_key="财务")
        session.add_all([dept, other])
        session.flush()
        session.add(
            User(
                username="member",
                username_key="member",
                password_hash=admin.password_hash,
                role="user",
                department_id=dept.id,
            )
        )
        other_id = other.id
    headers = auth(client, "member", database.admin_password)
    member = (client, headers, manager)
    conversation_id = new_conversation(member)
    first = staged(stage(member, conversation_id))
    second = staged(stage(member, conversation_id))
    manager.model = WriteModel(
        "document_upload",
        {
            "knowledge_base_id": base,
            "files": [
                {"staged_upload_id": first},
                {"staged_upload_id": second},
            ],
        },
    )
    response = submit(member, conversation_id, [first, second])
    run_id = int(response.json()["data"]["id"])
    client.portal.call(manager.execute, run_id)
    assert snapshot(member, run_id)["status"] == "completed", snapshot(member, run_id)
    with Session(database.engine) as session:
        result = session.scalars(select(ToolCall)).one().result_summary
        assert [item["status"] for item in result["items"]] == ["accepted", "name_conflict"]
        assert session.get(StorageObject, int(second)).status == "staged"
    manager.model = WriteModel(
        "document_upload",
        {
            "knowledge_base_id": base,
            "files": [
                {"staged_upload_id": second, "name": "另存.txt"},
            ],
        },
    )
    original_execute = Execution.execute_mutation

    async def revoke_target(self, state):
        with Session(database.engine) as session, session.begin():
            row = session.get(KnowledgeBase, int(base))
            row.type, row.department_id = "department", other_id
            row.row_version += 1
        return await original_execute(self, state)

    monkeypatch.setattr(Execution, "execute_mutation", revoke_target)
    response = submit(member, conversation_id)
    run_id = int(response.json()["data"]["id"])
    client.portal.call(manager.execute, run_id)
    assert snapshot(member, run_id)["failure_code"] == "RESOURCE_NOT_FOUND"
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 1
        assert session.get(StorageObject, int(second)).status == "staged"
