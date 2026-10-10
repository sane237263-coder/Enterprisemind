from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect
from test_ingestion import accepted, ingestion_runtime  # noqa: F401

from enterprise_mind.identity.models import Department, User
from enterprise_mind.ingestion.models import Document, OutboxEvent, StorageObject
from enterprise_mind.knowledge.models import DeletionBatch, KnowledgeBase

pytestmark = pytest.mark.integration


def test_upload_limits_malformed_body_and_invalid_folder(runtime, database):
    runtime.client.app.state.config.upload.max_bytes = 5
    result = runtime.upload([("files", ("large.txt", b"too large", "text/plain"))])
    assert result.json()["data"]["items"][0]["error_code"] == "FILE_TOO_LARGE"
    url = f"/api/v1/knowledge-bases/{runtime.base_id}/document-uploads"
    response = runtime.client.post(
        url,
        headers={
            **runtime.admin,
            "Idempotency-Key": "broken",
            "Content-Type": "multipart/form-data; boundary=x",
        },
        content=b"--x\r\nmalformed",
    )
    assert response.status_code == 400, response.text
    response = runtime.client.post(
        url, headers=runtime.admin, files=[("files", ("small.txt", b"text", "text/plain"))]
    )
    assert response.status_code == 422
    response = runtime.upload(
        [("files", ("small.txt", b"text", "text/plain"))], data={"folder_id": "999999"}
    )
    assert response.status_code == 404
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_credentials_revoked_during_upload_cannot_commit(runtime, database, monkeypatch):
    verify = runtime.worker.storage.verify

    def revoke_after_storage(*args):
        verify(*args)
        with Session(database.engine) as session, session.begin():
            session.get(User, database.user_id()).credential_version += 1

    monkeypatch.setattr(runtime.worker.storage, "verify", revoke_after_storage)
    response = runtime.upload(key="revoked")
    assert response.status_code == 401, response.text
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0
        assert session.scalar(select(func.count()).select_from(StorageObject)) == 0


def test_department_migration_revokes_document_and_ws_access(runtime, database):
    item = accepted(runtime.upload())
    with Session(database.engine) as session, session.begin():
        first = Department(name="甲", name_key="甲")
        second = Department(name="乙", name_key="乙")
        session.add_all([first, second])
        session.flush()
        admin = session.get(User, database.user_id())
        reader = User(
            username="部门读者",
            username_key="部门读者",
            password_hash=admin.password_hash,
            role="user",
            department_id=first.id,
        )
        session.add(reader)
        base = session.get(KnowledgeBase, int(runtime.base_id))
        base.type, base.department_id = "department", first.id
        second_id = second.id
    login = runtime.client.post(
        "/api/v1/auth/login", json={"username": "部门读者", "password": database.admin_password}
    )
    token = login.json()["data"]["access_token"]
    headers = {"Authorization": "Bearer " + token}
    url = f"/api/v1/documents/{item['document_id']}"
    assert runtime.client.get(url, headers=headers).status_code == 200
    with runtime.client.websocket_connect("/api/v1/ws/document-processing") as socket:
        socket.send_json(
            {"type": "subscribe", "access_token": token, "document_ids": [item["document_id"]]}
        )
        assert socket.receive_json()["type"] == "document.processing"
        response = runtime.client.patch(
            f"/api/v1/knowledge-bases/{runtime.base_id}",
            headers=runtime.admin,
            json={"row_version": 1, "department_id": str(second_id)},
        )
        assert response.status_code == 200, response.text
        socket.send_text("ping")
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert runtime.client.get(url, headers=headers).status_code == 404
    assert runtime.client.get(url + "/processing", headers=headers).status_code == 404
    assert runtime.upload(headers=headers).status_code == 404


def test_purging_blocks_lease_and_late_results(runtime, database):
    item = accepted(runtime.upload())
    lease = runtime.run(runtime.worker.claim, int(item["processing_task_id"]))
    with Session(database.engine) as session, session.begin():
        batch = DeletionBatch(
            root_type="document",
            root_id=int(item["document_id"]),
            root_name="test",
            deleted_at=datetime.now(UTC),
            purge_after=datetime.now(UTC) + timedelta(days=30),
            status="purging",
        )
        session.add(batch)
        session.flush()
        document = session.get(Document, int(item["document_id"]))
        document.deleted_at, document.purge_after, document.deletion_batch_id = (
            batch.deleted_at,
            batch.purge_after,
            batch.id,
        )
    assert runtime.run(runtime.worker.renew, lease) is False
    assert runtime.run(runtime.worker.persist, lease, None, "PARSE_FAILED") is False
    assert runtime.run(runtime.worker.claim, int(item["processing_task_id"])) is None
    assert (
        runtime.client.get(
            f"/api/v1/documents/{item['document_id']}", headers=runtime.admin
        ).status_code
        == 404
    )


def test_outbox_cleanup_keeps_unfinished_supported_work(runtime, database):
    accepted(runtime.upload())
    runtime.run(runtime.dispatcher.dispatch_once)
    with Session(database.engine) as session, session.begin():
        session.scalars(select(OutboxEvent)).one().published_at = datetime.now(UTC) - timedelta(
            days=8
        )
    runtime.run(runtime.dispatcher.expire_published)
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
    runtime.run(runtime.worker.run_once)
    runtime.run(runtime.dispatcher.expire_published)
    # Parsed tasks now retain their event while configured indexing can still resume them.
    with Session(database.engine) as session, session.begin():
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        from enterprise_mind.ingestion.models import ProcessingUnit

        for unit in session.scalars(
            select(ProcessingUnit).where(ProcessingUnit.status == "pending")
        ):
            unit.status = "failed"
    runtime.run(runtime.dispatcher.expire_published)
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_outbox_recovery_batch_makes_progress_past_recently_dispatched_work(runtime, database):
    for number in range(4):
        accepted(
            runtime.upload(
                [("files", (f"recovery-{number}.txt", b"pending text", "text/plain"))],
                key=f"recovery-{number}",
            )
        )
    runtime.run(runtime.dispatcher.dispatch_once)
    with Session(database.engine) as session, session.begin():
        events = session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)).all()
        recent_id = events[0].id
        for event in events[1:]:
            event.published_at = datetime.now(UTC) - timedelta(days=1)
    runtime.dispatcher.config.outbox.claim_batch_size = 1
    for _ in range(4):
        runtime.run(runtime.dispatcher.recover)
    with Session(database.engine) as session:
        events = session.scalars(select(OutboxEvent)).all()
        assert session.get(OutboxEvent, recent_id).status == "published"
        assert all(event.status == "pending" for event in events if event.id != recent_id)
