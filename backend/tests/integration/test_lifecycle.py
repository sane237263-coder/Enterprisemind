import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_ingestion import accepted, ingestion_runtime  # noqa: F401
from test_rag_pipeline import indexed, retrieve, upload_index  # noqa: F401, F811

from enterprise_mind.ingestion.cleanup import CleanupWorker
from enterprise_mind.ingestion.models import (
    Document,
    Folder,
    OutboxEvent,
    ProcessingUnit,
    Revision,
    StorageObject,
)
from enterprise_mind.knowledge.models import DeletionBatch
from enterprise_mind.knowledge.pending_models import PendingAction

pytestmark = pytest.mark.integration


def post(runtime, path, body, key=None):
    return runtime.client.post(
        "/api/v1/" + path,
        headers={**runtime.admin, "Idempotency-Key": key or uuid.uuid4().hex},
        json=body,
    )


def data(response, status=200):
    assert response.status_code == status, response.text
    return response.json()["data"]


def folder(runtime, name, parent=None):
    return data(
        post(
            runtime,
            f"knowledge-bases/{runtime.base_id}/folders",
            {"name": name, "parent_id": parent},
        ),
        201,
    )


def confirm(runtime, action, key=None, decision="accept"):
    return post(
        runtime,
        f"pending-actions/{action['pending_action_id']}/responses",
        {"decision": decision},
        key,
    )


def deletion(runtime, kind, identifier, version=1):
    return data(post(runtime, f"{kind}/{identifier}/deletion-requests", {"row_version": version}))


def test_folder_move_cycle_scope_and_empty_delete_replay(runtime):
    a = folder(runtime, "A")
    b = folder(runtime, "B", a["id"])
    response = runtime.client.patch(
        f"/api/v1/folders/{a['id']}",
        headers=runtime.admin,
        json={"row_version": 1, "parent_id": b["id"]},
    )
    assert response.json()["error"]["code"] == "FOLDER_CYCLE"
    assert folder(runtime, "C")["row_version"] == 1
    conflict = post(runtime, f"knowledge-bases/{runtime.base_id}/folders", {"name": " Ａ "})
    assert conflict.json()["error"]["code"] == "NAME_CONFLICT"
    path = f"folders/{b['id']}/deletion-requests"
    first = data(post(runtime, path, {"row_version": 1}, "empty-delete"))
    assert first["status"] == "recycle_bin"
    assert data(post(runtime, path, {"row_version": 1}, "empty-delete")) == first
    assert (
        data(post(runtime, f"folders/{b['id']}/restore", {"row_version": 2}))["scope"] == "active"
    )


def test_confirmation_binds_subtree_and_replays_only_identical_response(runtime, database):
    root = folder(runtime, "root")
    child = folder(runtime, "child", root["id"])
    action = deletion(runtime, "folders", root["id"])
    folder(runtime, "added later", root["id"])
    response = confirm(runtime, action)
    assert response.json()["error"]["code"] == "PENDING_ACTION_BINDING_CHANGED"
    data(confirm(runtime, action, decision="reject"))
    action = deletion(runtime, "folders", root["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: confirm(runtime, action, "same-response"), range(2)))
    assert data(responses[0]) == data(responses[1])
    assert data(responses[0])["status"] == "accepted"
    assert confirm(runtime, action).json()["error"]["code"] == "PENDING_ACTION_RESOLVED"
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(DeletionBatch)) == 1
        assert session.get(Folder, int(child["id"])).deleted_at is not None


def test_recycle_lists_expose_only_actual_batch_roots(runtime):
    root = folder(runtime, "完整恢复根目录")
    child = folder(runtime, "随父目录回收", root["id"])
    independent = folder(runtime, "提前独立删除", root["id"])
    deletion(runtime, "folders", independent["id"])
    doc = accepted(runtime.upload(data={"folder_id": child["id"]}))
    data(confirm(runtime, deletion(runtime, "folders", root["id"])))
    prefix = f"/api/v1/knowledge-bases/{runtime.base_id}"
    folders = data(runtime.client.get(prefix + "/folders?scope=recycle_bin", headers=runtime.admin))
    flags = {row["id"]: row["is_deletion_root"] for row in folders}
    assert flags == {root["id"]: True, child["id"]: False, independent["id"]: True}
    documents = data(
        runtime.client.get(prefix + "/documents?scope=recycle_bin", headers=runtime.admin)
    )
    assert documents[0]["id"] == doc["document_id"]
    assert documents[0]["is_deletion_root"] is False


def test_restore_batch_excludes_earlier_deletion_and_conflicts_are_atomic(runtime, database):
    root = folder(runtime, "root")
    old = folder(runtime, "old", root["id"])
    deletion(runtime, "folders", old["id"])
    doc = accepted(runtime.upload(data={"folder_id": root["id"]}))
    data(confirm(runtime, deletion(runtime, "folders", root["id"])))
    folder(runtime, "root")
    failed = post(runtime, f"folders/{root['id']}/restore", {"row_version": 2})
    assert failed.json()["error"]["code"] == "NAME_CONFLICT"
    data(post(runtime, f"folders/{root['id']}/restore", {"row_version": 2, "name": "restored"}))
    with Session(database.engine) as session:
        assert session.get(Folder, int(old["id"])).deleted_at is not None
        assert session.get(Document, int(doc["document_id"])).deleted_at is None
    recycle = runtime.client.get(
        f"/api/v1/knowledge-bases/{runtime.base_id}/folders?scope=recycle_bin",
        headers=runtime.admin,
    )
    assert [item["id"] for item in data(recycle)] == [old["id"]]


def test_overwrite_staging_replay_failure_preserves_active_then_retry(indexed, database):  # noqa: F811
    runtime, _, _, embedding, _ = indexed
    item = upload_index(indexed)
    doc_id = item["document_id"]
    with Session(database.engine) as session:
        active = session.get(Document, int(doc_id)).active_revision_id

    def stage():
        return runtime.client.post(
            f"/api/v1/documents/{doc_id}/overwrite-requests",
            headers={**runtime.admin, "Idempotency-Key": "overwrite"},
            files=[("files", ("next.txt", "ABC-123 新内容".encode(), "text/plain"))],
            data={"row_version": "1"},
        )

    action = data(stage(), 202)
    assert data(stage(), 202)["id"] == action["id"]
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Revision)) == 1
    accepted_action = data(confirm(runtime, action))
    task_id = int(accepted_action["result"]["processing_task_id"])
    embedding.fail = True
    with Session(database.engine) as session:
        event = session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.deduplication_key == f"document.processing:{task_id}"
            )
        )
    assert runtime.run(runtime.worker.handle_event, event)
    with Session(database.engine) as session:
        doc = session.get(Document, int(doc_id))
        assert doc.active_revision_id == active and doc.processing_status == "failed"
        assert (
            session.scalar(
                select(ProcessingUnit.attempt_count).where(
                    ProcessingUnit.revision_id != active, ProcessingUnit.stage == "parse"
                )
            )
            == 1
        )
    assert retrieve(indexed).status == "ok"
    retry = data(
        post(runtime, f"documents/{doc_id}/processing/retry", {"row_version": 2}, "retry"), 202
    )
    embedding.fail = False
    with Session(database.engine) as session:
        event = session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.deduplication_key
                == f"document.processing:{retry['processing_task_id']}"
            )
        )
    assert runtime.run(runtime.worker.handle_event, event)
    with Session(database.engine) as session:
        doc = session.get(Document, int(doc_id))
        assert doc.active_revision_id != active and doc.processing_status == "success"
        assert (
            session.scalar(
                select(ProcessingUnit.attempt_count).where(
                    ProcessingUnit.revision_id == doc.active_revision_id,
                    ProcessingUnit.stage == "parse",
                )
            )
            == 1
        )
        assert session.get(Revision, active).retired_at is not None


def test_purge_waits_for_lease_and_retries_object_failure(runtime, database, monkeypatch):
    item = accepted(runtime.upload())
    lease = runtime.run(runtime.worker.claim, int(item["processing_task_id"]))
    assert lease is not None
    data(confirm(runtime, deletion(runtime, "documents", item["document_id"])))
    action = data(
        post(runtime, f"documents/{item['document_id']}/purge-requests", {"row_version": 2})
    )
    data(confirm(runtime, action))
    cleanup = CleanupWorker(runtime.worker.sessions, runtime.worker.storage)
    with Session(database.engine) as session:
        event_id = session.scalar(
            select(OutboxEvent.id).where(OutboxEvent.event_type == "document.purge.requested")
        )
    assert not runtime.run(cleanup.handle, event_id)
    assert not runtime.run(runtime.worker.persist, lease, None, "PARSE_FAILED")
    with Session(database.engine) as session, session.begin():
        unit = session.get(ProcessingUnit, lease.unit_id)
        unit.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert not runtime.run(cleanup.handle, event_id)
    with Session(database.engine) as session:
        storage_event = session.scalar(
            select(OutboxEvent.id).where(OutboxEvent.event_type == "storage.delete.requested")
        )
    original = runtime.worker.storage.remove

    def fail(_):
        raise OSError("test storage unavailable")

    monkeypatch.setattr(runtime.worker.storage, "remove", fail)
    assert not runtime.run(cleanup.handle, storage_event)
    with Session(database.engine) as session:
        assert session.get(Document, int(item["document_id"])) is not None
        assert session.scalars(select(StorageObject)).one().status == "error"
    monkeypatch.setattr(runtime.worker.storage, "remove", original)
    assert runtime.run(cleanup.handle, storage_event)
    assert runtime.run(cleanup.handle, event_id)
    assert runtime.run(cleanup.handle, event_id)
    with Session(database.engine) as session:
        assert session.get(Document, int(item["document_id"])) is None
        assert session.scalar(select(func.count()).select_from(StorageObject)) == 0


def test_expiry_cannot_execute_and_scheduler_cleans_staging(runtime, database):
    item = accepted(runtime.upload())
    action = deletion(runtime, "documents", item["document_id"])
    with Session(database.engine) as session, session.begin():
        pending = session.get(PendingAction, int(action["id"]))
        pending.created_at = datetime.now(UTC) - timedelta(hours=2)
        pending.expires_at = datetime.now(UTC) - timedelta(hours=1)
    assert confirm(runtime, action).json()["error"]["code"] == "PENDING_ACTION_EXPIRED"
    with Session(database.engine) as session:
        assert session.get(Document, int(item["document_id"])).deleted_at is None


def test_restore_resumes_activation(indexed, database):  # noqa: F811
    runtime, *_ = indexed
    item = accepted(runtime.upload())
    data(confirm(runtime, deletion(runtime, "documents", item["document_id"])))
    with Session(database.engine) as session:
        event_id = session.scalar(
            select(OutboxEvent.id).where(OutboxEvent.event_type == "document.processing.requested")
        )
    assert not runtime.run(runtime.worker.handle_event, event_id)
    with Session(database.engine) as session:
        assert session.get(Document, int(item["document_id"])).active_revision_id is None
    data(post(runtime, f"documents/{item['document_id']}/restore", {"row_version": 2}))
    assert runtime.run(runtime.worker.handle_event, event_id)
    with Session(database.engine) as session:
        assert session.get(Document, int(item["document_id"])).active_revision_id is not None


def test_partial_retry_keeps_ready_chunks_and_does_not_redo_successful_units(indexed, database):  # noqa: F811
    from enterprise_mind.rag.models import Chunk

    runtime, _, _, embedding, _ = indexed
    embedding.fail_term = "FAIL_BLOCK"
    item = upload_index(indexed, body="ABC-123 可用内容。" * 15 + "\n\n" + "FAIL_BLOCK 失败。" * 15)
    with Session(database.engine) as session:
        document = session.get(Document, int(item["document_id"]))
        assert document.processing_status == "partial_success"
        active = document.active_revision_id
        ready = set(session.scalars(select(Chunk.id).where(Chunk.status == "ready")))
        previous_attempts = dict(
            session.execute(
                select(ProcessingUnit.id, ProcessingUnit.attempt_count).where(
                    ProcessingUnit.status == "success", ProcessingUnit.stage != "activate"
                )
            ).all()
        )
    retried = data(
        post(runtime, f"documents/{item['document_id']}/processing/retry", {"row_version": 1}), 202
    )
    with Session(database.engine) as session:
        document = session.get(Document, int(item["document_id"]))
        assert (
            document.processing_status == "partial_success"
            and document.active_revision_id == active
        )
        event = session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.deduplication_key
                == f"document.processing:{retried['processing_task_id']}"
            )
        )
    assert retrieve(indexed).status == "ok"
    embedding.fail_term = None
    assert runtime.run(runtime.worker.handle_event, event)
    with Session(database.engine) as session:
        document = session.get(Document, int(item["document_id"]))
        assert document.processing_status == "success"
        assert ready <= set(session.scalars(select(Chunk.id).where(Chunk.status == "ready")))
        assert (
            dict(
                session.execute(
                    select(ProcessingUnit.id, ProcessingUnit.attempt_count).where(
                        ProcessingUnit.id.in_(previous_attempts)
                    )
                ).all()
            )
            == previous_attempts
        )


def test_automatic_purge_preserves_younger_independent_batch_and_staging_expires(runtime, database):
    root = folder(runtime, "root")
    child = folder(runtime, "independent", root["id"])
    deletion(runtime, "folders", child["id"])
    deletion(runtime, "folders", root["id"])
    with Session(database.engine) as session, session.begin():
        batch = session.scalar(
            select(DeletionBatch).where(
                DeletionBatch.root_id == int(root["id"]), DeletionBatch.root_type == "folder"
            )
        )
        batch.deleted_at = datetime.now(UTC) - timedelta(days=31)
        batch.purge_after = datetime.now(UTC) - timedelta(days=1)
        row = session.get(Folder, int(root["id"]))
        row.deleted_at, row.purge_after = batch.deleted_at, batch.purge_after
    runtime.run(runtime.dispatcher.dispatch_once)
    with Session(database.engine) as session:
        assert session.get(Folder, int(root["id"])) is not None
        assert (
            session.scalar(
                select(OutboxEvent.id).where(OutboxEvent.event_type == "document.purge.requested")
            )
            is None
        )
    item = accepted(runtime.upload())
    staged = runtime.client.post(
        f"/api/v1/documents/{item['document_id']}/overwrite-requests",
        headers={**runtime.admin, "Idempotency-Key": "expiring-stage"},
        files=[("files", ("next.txt", b"candidate", "text/plain"))],
        data={"row_version": "1"},
    )
    action = data(staged, 202)
    data(confirm(runtime, action, decision="reject"))
    runtime.run(runtime.dispatcher.dispatch_once)
    with Session(database.engine) as session:
        event = session.scalar(
            select(OutboxEvent.id).where(OutboxEvent.event_type == "storage.delete.requested")
        )
        assert event is not None
    cleanup = CleanupWorker(runtime.worker.sessions, runtime.worker.storage)
    assert runtime.run(cleanup.handle, event)
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(StorageObject)) == 1


def test_permission_change_invalidates_confirmation_and_owned_action_stays_private(
    runtime, database
):
    from test_management import auth

    from enterprise_mind.identity.models import Department, User

    with Session(database.engine) as session, session.begin():
        admin = session.get(User, database.user_id())
        department = Department(name="department", name_key="department")
        session.add(department)
        session.flush()
        user = User(
            username="member",
            username_key="member",
            role="user",
            department_id=department.id,
            password_hash=admin.password_hash,
        )
        session.add(user)
        session.flush()
        user_id = user.id
    item = accepted(runtime.upload())
    action = deletion(runtime, "documents", item["document_id"])
    member = auth(runtime.client, "member", database.admin_password)
    assert (
        runtime.client.get(f"/api/v1/pending-actions/{action['id']}", headers=member).status_code
        == 404
    )
    own = runtime.client.post(
        f"/api/v1/documents/{item['document_id']}/deletion-requests",
        headers={**member, "Idempotency-Key": "member-action"},
        json={"row_version": 1},
    )
    assert own.status_code == 200
    with Session(database.engine) as session, session.begin():
        session.get(User, user_id).role = "admin"
    response = runtime.client.post(
        f"/api/v1/pending-actions/{own.json()['data']['id']}/responses",
        headers={**member, "Idempotency-Key": "member-response"},
        json={"decision": "accept"},
    )
    assert (
        response.status_code == 409
        and response.json()["error"]["code"] == "PENDING_ACTION_BINDING_CHANGED"
    )
