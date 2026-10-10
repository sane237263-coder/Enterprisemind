import asyncio
import hashlib
import io
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pymupdf
import pytest
from fastapi.testclient import TestClient
from minio.error import S3Error
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from enterprise_mind.agent.checkpoints import initialize_checkpoints
from enterprise_mind.app import create_app
from enterprise_mind.identity.models import Department, User
from enterprise_mind.infrastructure.models import IdempotencyRecord
from enterprise_mind.ingestion.dispatch import Dispatcher, sweep_orphans
from enterprise_mind.ingestion.models import (
    ContentElement,
    Document,
    OutboxEvent,
    ProcessingTask,
    ProcessingUnit,
    SourceAnchor,
    SourceUnit,
    StorageObject,
)
from enterprise_mind.ingestion.parsing import ParsedDocument, ParsedSource
from enterprise_mind.ingestion.worker import IngestionWorker

pytestmark = pytest.mark.integration


@dataclass
class Runtime:
    client: TestClient
    admin: dict
    base_id: str
    worker: IngestionWorker
    dispatcher: Dispatcher

    def upload(self, files=None, *, key=None, data=None, headers=None):
        return self.client.post(
            f"/api/v1/knowledge-bases/{self.base_id}/document-uploads",
            headers={**(headers or self.admin), "Idempotency-Key": key or uuid.uuid4().hex},
            files=files
            or [("files", ("sample.txt", "来源测试\n\n第二段。".encode(), "text/plain"))],
            data=data,
        )

    def run(self, function, *args):
        return self.client.portal.call(function, *args)


def worker_with_windows_parser_loop(worker):
    if sys.platform == "win32":
        # The API/checkpoint portal needs Selector; the parser child process
        # needs Proactor. Production runs these in separate processes.
        parser = worker.parse

        async def parse_on_worker_loop(lease):
            return await asyncio.to_thread(lambda: asyncio.run(parser(lease)))

        worker.parse = parse_on_worker_loop
    return worker


@pytest.fixture(name="runtime")
def ingestion_runtime(database, ingestion_settings):
    database.initialize()
    asyncio.run(initialize_checkpoints(database.url), loop_factory=asyncio.SelectorEventLoop)
    settings, _, _ = ingestion_settings
    app = create_app(database.settings, settings)
    with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "初始管理员", "password": database.admin_password},
        )
        admin = {"Authorization": "Bearer " + login.json()["data"]["access_token"]}
        base = client.post(
            "/api/v1/knowledge-bases",
            headers={**admin, "Idempotency-Key": uuid.uuid4().hex},
            json={"name": "测试知识库", "type": "public"},
        )
        assert base.status_code == 201, base.text
        yield Runtime(
            client,
            admin,
            base.json()["data"]["id"],
            worker_with_windows_parser_loop(
                IngestionWorker(
                    app.state.sessions, app.state.storage, app.state.queue, app.state.config.worker
                )
            ),
            Dispatcher(app.state.sessions, app.state.queue, app.state.config),
        )


def accepted(response):
    assert response.status_code == 202, response.text
    assert response.json()["data"]["items"][0]["status"] == "accepted", response.text
    return response.json()["data"]["items"][0]


def test_upload_commit_parse_anchors_and_pending_index(runtime, database, ingestion_settings):
    result = accepted(runtime.upload())
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 1
        original = session.scalars(select(StorageObject)).one()
        assert (
            original.content_sha256 == hashlib.sha256("来源测试\n\n第二段。".encode()).hexdigest()
        )
        runtime.worker.storage.verify(
            original.object_key, original.size_bytes, original.content_sha256
        )
        assert session.scalars(select(OutboxEvent)).one().status == "pending"
    assert runtime.run(runtime.dispatcher.dispatch_once) == 1
    _, _, redis = ingestion_settings
    entries = redis.xrange(runtime.worker.queue.stream)
    assert len(entries) == 1 and set(entries[0][1]) == {"event_id"}
    assert runtime.run(runtime.worker.run_once)
    state = runtime.client.get(
        f"/api/v1/documents/{result['document_id']}/processing", headers=runtime.admin
    )
    assert state.status_code == 200, state.text
    assert state.json()["data"]["current_stage"] == "chunk"
    assert state.json()["data"]["processing_status"] == "processing"
    with Session(database.engine) as session:
        document = session.scalars(select(Document)).one()
        assert document.active_revision_id is None
        assert session.scalars(select(SourceUnit)).one().extracted_text == "来源测试\n\n第二段。"
        anchors = session.scalars(select(SourceAnchor)).all()
        assert len(anchors) == 2
        for anchor in anchors:
            assert (
                anchor.source_text_sha256 == hashlib.sha256(anchor.source_text.encode()).hexdigest()
            )
        event = session.scalars(select(OutboxEvent)).one()
        assert runtime.run(runtime.worker.handle_event, event.id)
        assert session.scalar(select(func.count()).select_from(SourceUnit)) == 1
    for forbidden in ("object_key", "original/", "password", "lease_owner", "source_text"):
        assert forbidden not in state.text
    assert redis.xlen(runtime.worker.queue.stream) == 0


def test_batch_validation_name_conflict_and_durable_replay(runtime, database):
    files = [
        ("files", ("a.md", b"# Header\n", "text/markdown")),
        ("files", ("bad.exe", b"binary", "application/octet-stream")),
        ("files", ("fake.pdf", b"not a pdf", "application/pdf")),
    ]
    first = runtime.upload(files, key="durable-upload")
    assert first.status_code == 202, first.text
    items = first.json()["data"]["items"]
    assert [item["status"] for item in items] == ["accepted", "rejected", "rejected"]
    assert [item.get("error_code") for item in items[1:]] == [
        "UNSUPPORTED_FILE_TYPE",
        "FILE_CONTENT_MISMATCH",
    ]
    assert runtime.upload(files, key="durable-upload").json()["data"] == first.json()["data"]
    assert runtime.upload(key="durable-upload").status_code == 409
    conflict = runtime.upload(files[:1]).json()["data"]["items"][0]
    assert conflict["status"] == "name_conflict"
    assert conflict["document_id"] == items[0]["document_id"]
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 1
        record = session.scalars(
            select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key == "durable-upload")
        ).one()
        assert "original/" not in str(record.response_data)


def test_concurrent_same_key_creates_one_document(runtime, database):
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: runtime.upload(key="parallel"), range(2)))
    assert all(r.status_code == 202 for r in responses), [r.text for r in responses]
    assert responses[0].json()["data"] == responses[1].json()["data"]
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 1
        assert session.scalar(select(func.count()).select_from(ProcessingTask)) == 1


def test_storage_failure_rolls_back_batch_and_key_remains_retryable(runtime, database, monkeypatch):
    original = runtime.worker.storage.verify
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("private detail must not escape")
        return original(*args)

    monkeypatch.setattr(runtime.worker.storage, "verify", fail_second)
    files = [("files", (name, b"body", "text/plain")) for name in ("a.txt", "b.txt")]
    response = runtime.upload(files, key="storage-retry")
    assert response.status_code == 503, response.text
    assert "private detail" not in response.text
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0
        assert (
            session.scalar(
                select(IdempotencyRecord.id).where(
                    IdempotencyRecord.idempotency_key == "storage-retry"
                )
            )
            is None
        )
    monkeypatch.setattr(runtime.worker.storage, "verify", original)
    assert all(
        i["status"] == "accepted"
        for i in runtime.upload(files, key="storage-retry").json()["data"]["items"]
    )


def test_redis_failure_restart_recovery_and_duplicate_delivery(
    runtime, database, ingestion_settings, monkeypatch
):
    accepted(runtime.upload())

    async def unavailable(*args):
        raise ConnectionError("queue unavailable")

    publish = runtime.worker.queue.publish
    monkeypatch.setattr(runtime.worker.queue, "publish", unavailable)
    assert runtime.run(runtime.dispatcher.dispatch_once) == 0
    with Session(database.engine) as session, session.begin():
        event = session.scalars(select(OutboxEvent)).one()
        assert event.status == "failed" and event.attempt_count == 1
        event.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr(runtime.worker.queue, "publish", publish)
    assert runtime.run(runtime.dispatcher.dispatch_once) == 1
    _, _, redis = ingestion_settings
    redis.delete(runtime.worker.queue.stream)  # Redis loses its transport state.
    with Session(database.engine) as session, session.begin():
        event = session.scalars(select(OutboxEvent)).one()
        event.published_at = datetime.now(UTC) - timedelta(minutes=10)
    assert runtime.run(runtime.dispatcher.dispatch_once) == 1
    assert runtime.run(runtime.worker.run_once)
    assert runtime.run(runtime.dispatcher.dispatch_once) == 0
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(SourceUnit)) == 1


def test_expired_lease_rejects_late_result_and_reclaim_renews(runtime, database):
    task_id = int(accepted(runtime.upload())["processing_task_id"])
    stale = runtime.run(runtime.worker.claim, task_id)
    assert runtime.run(runtime.worker.renew, stale)
    with Session(database.engine) as session, session.begin():
        unit = session.get(ProcessingUnit, stale.unit_id)
        unit.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    parsed = ParsedDocument(sources=[ParsedSource(kind="document", ordinal=0, text="stale")])
    assert runtime.run(runtime.worker.persist, stale, parsed) is False
    assert runtime.run(runtime.dispatcher.recover) == 1
    fresh = runtime.run(runtime.worker.claim, task_id)
    assert fresh.owner != stale.owner
    assert runtime.run(runtime.worker.persist, stale, parsed) is False
    assert runtime.run(runtime.worker.execute, fresh)
    with Session(database.engine) as session:
        assert session.get(ProcessingUnit, fresh.unit_id).attempt_count == 2
        assert session.scalars(select(SourceUnit)).one().extracted_text != "stale"


@pytest.mark.parametrize("extension", ["md", "csv", "pdf", "damaged_pdf"])
def test_real_parser_process_formats(runtime, database, extension):
    if extension == "md":
        data, mime = (
            b"# Heading\n\n```python\nprint(1)\n```\n\n|A|B|\n|-|-|\n|1|2|\n",
            "text/markdown",
        )
    elif extension == "csv":
        data, mime = "名称,值\n甲,1\n".encode(), "text/csv"
    elif extension == "pdf":
        with pymupdf.open() as document:
            document.new_page().insert_text((30, 50), "Native test")
            document.new_page()
            data = document.tobytes()
        mime = "application/pdf"
    else:
        data, mime, extension = b"%PDF-1.7\ndamaged", "application/pdf", "pdf"
    item = accepted(runtime.upload([("files", ("file." + extension, data, mime))]))
    runtime.run(runtime.dispatcher.dispatch_once)
    assert runtime.run(runtime.worker.run_once)
    with Session(database.engine) as session:
        document = session.get(Document, int(item["document_id"]))
        units = session.scalars(select(ProcessingUnit)).all()
        if data.startswith(b"%PDF-1.7\ndamaged"):
            assert document.processing_status == "failed"
            assert len(units) == 1 and units[0].error_code == "PARSE_FAILED"
        else:
            assert document.processing_status == "processing"
            assert any(u.stage == "chunk" and u.status == "pending" for u in units)
            assert all(
                u.status == "success" for u in units if u.stage in {"parse", "table", "code"}
            )
            if extension == "pdf":
                assert any(u.stage == "ocr" and u.status == "pending" for u in units)
            if extension in {"md", "csv"}:
                assert session.scalar(
                    select(ContentElement.id).where(ContentElement.element_type == "table")
                )


def test_read_permissions_and_websocket_rechecks_account(runtime, database):
    item = accepted(runtime.upload())
    with Session(database.engine) as session, session.begin():
        department = Department(name="读者部门", name_key="读者部门")
        session.add(department)
        session.flush()
        admin = session.get(User, database.user_id())
        user = User(
            username="读者",
            username_key="读者",
            password_hash=admin.password_hash,
            role="user",
            department_id=department.id,
        )
        session.add(user)
        session.flush()
        user_id = user.id
    response = runtime.client.post(
        "/api/v1/auth/login", json={"username": "读者", "password": database.admin_password}
    )
    token = response.json()["data"]["access_token"]
    headers = {"Authorization": "Bearer " + token}
    url = f"/api/v1/knowledge-bases/{runtime.base_id}/documents"
    assert runtime.client.get(url, headers=headers).status_code == 200
    # Requirements grant ordinary users content management in every visible knowledge base.
    assert (
        runtime.upload(
            [("files", ("reader.txt", b"reader upload", "text/plain"))], headers=headers
        ).status_code
        == 202
    )
    with runtime.client.websocket_connect("/api/v1/ws/document-processing") as socket:
        socket.send_json(
            {"type": "subscribe", "access_token": token, "document_ids": [item["document_id"]]}
        )
        assert socket.receive_json()["data"]["document_id"] == item["document_id"]
        with Session(database.engine) as session, session.begin():
            session.get(User, user_id).status = "disabled"
        socket.send_text("ping")
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008
    assert runtime.client.get(url, headers=headers).status_code == 403


def test_minio_identity_cannot_create_bucket_and_orphan_cleanup_is_scoped(
    runtime, database, ingestion_settings, monkeypatch
):
    from enterprise_mind.ingestion import dispatch

    settings, root, _ = ingestion_settings
    accepted(runtime.upload())
    with pytest.raises(S3Error):
        runtime.worker.storage.client.make_bucket("forbidden-" + uuid.uuid4().hex)
    key = "original/20200101T000000Z/" + uuid.uuid4().hex
    root.put_object(settings.minio_bucket, key, io.BytesIO(b"orphan"), 6)
    root.put_object(settings.minio_bucket, "outside-prefix", io.BytesIO(b"keep"), 4)

    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=2)

    monkeypatch.setattr(dispatch, "datetime", Future)
    assert runtime.run(sweep_orphans, runtime.worker.sessions, runtime.worker.storage, 86400) == 1
    assert root.stat_object(settings.minio_bucket, "outside-prefix").size == 4
    with Session(database.engine) as session:
        original = session.scalars(select(StorageObject)).one()
        assert (
            root.stat_object(settings.minio_bucket, original.object_key).size == original.size_bytes
        )
