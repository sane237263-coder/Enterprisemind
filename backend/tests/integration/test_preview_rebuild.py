# ruff: noqa: F811
from datetime import UTC, datetime, timedelta

import pytest
from media_samples import docx_bytes, pptx_bytes, xlsx_bytes
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_ingestion import ingestion_runtime  # noqa: F401
from test_lifecycle import confirm, data, deletion
from test_media_pipeline import process
from test_rag_pipeline import indexed, retrieve  # noqa: F401

from enterprise_mind.identity.models import Department, User
from enterprise_mind.ingestion.models import (
    ContentElement,
    Document,
    DocumentArtifact,
    OutboxEvent,
    ProcessingTask,
    ProcessingUnit,
    Revision,
    SourceAnchor,
    SourceUnit,
    StorageObject,
)
from enterprise_mind.knowledge.models import KnowledgeBase
from enterprise_mind.rag.models import Chunk, ChunkEmbedding

pytestmark = pytest.mark.integration


def descriptor(runtime, identifier):
    return data(
        runtime.client.get(f"/api/v1/documents/{identifier}/preview", headers=runtime.admin)
    )


def rebuild(runtime, identifier, *, key="rebuild-test", body=None):
    current = descriptor(runtime, identifier)
    return runtime.client.post(
        f"/api/v1/documents/{identifier}/preview/rebuild",
        headers={**runtime.admin, "Idempotency-Key": key},
        json=body or {"row_version": current["row_version"], "revision_id": current["revision_id"]},
    )


def event_for(database, task_id):
    with Session(database.engine) as session:
        return session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.deduplication_key == "document.processing:" + task_id
            )
        )


def indexed_snapshot(database, identifier):
    with Session(database.engine) as session:
        document = session.get(Document, int(identifier))
        revision = session.get(Revision, document.active_revision_id)
        rows = {
            model.__tablename__: [
                tuple(row) for row in session.execute(select(model.__table__).order_by(model.id))
            ]
            for model in (SourceUnit, ContentElement, SourceAnchor, Chunk, ChunkEmbedding)
        }
        return (
            document.active_revision_id,
            document.latest_revision_no,
            document.processing_status,
            document.row_version,
            document.updated_at,
            revision.original_object_id,
            revision.processing_status,
            revision.activated_at,
            rows,
        )


@pytest.mark.parametrize(
    "extension,builder",
    [
        ("docx", docx_bytes),
        ("pptx", pptx_bytes),
        ("xlsx", xlsx_bytes),
        ("md", lambda: b"# ABC-123\n\nDocument text."),
    ],
)
def test_historical_preview_rebuild_keeps_sources_indexes_and_idempotency(
    indexed, database, extension, builder
):
    runtime = indexed[0]
    identifier = process(runtime, extension, builder())["document_id"]
    before = indexed_snapshot(database, identifier)
    with Session(database.engine) as session, session.begin():
        session.execute(
            delete(DocumentArtifact).where(DocumentArtifact.artifact_type == "preview_html")
        )
    current = descriptor(runtime, identifier)
    assert not current["ready"]
    body = {"row_version": current["row_version"], "revision_id": current["revision_id"]}
    response = data(rebuild(runtime, identifier, body=body), 202)
    assert data(rebuild(runtime, identifier, body=body), 202) == response
    assert rebuild(runtime, identifier, key="another").status_code == 409
    assert runtime.run(
        runtime.worker.handle_event, event_for(database, response["processing_task_id"])
    )
    after = descriptor(runtime, identifier)
    assert after["ready"] and after["rebuild_task"]["status"] == "success"
    content = runtime.client.get(after["artifacts"][0]["content_url"], headers=runtime.admin)
    assert content.status_code == 200 and "ABC-123" in content.text
    assert indexed_snapshot(database, identifier) == before
    assert retrieve(indexed).contexts
    processing = data(
        runtime.client.get(f"/api/v1/documents/{identifier}/processing", headers=runtime.admin)
    )
    assert processing["task_status"] == "success" and processing["current_stage"] == "activate"


def test_failed_preview_preserves_previous_artifact_and_can_rebuild(indexed, database, monkeypatch):
    runtime = indexed[0]
    identifier = process(runtime, "docx", docx_bytes())["document_id"]
    before = indexed_snapshot(database, identifier)
    previous = descriptor(runtime, identifier)["artifacts"]
    import enterprise_mind.ingestion.preview_rebuild as service

    original = service.run_parser

    async def unavailable(*args, **kwargs):
        raise ValueError("private detail must not leave worker")

    monkeypatch.setattr(service, "run_parser", unavailable)
    task = data(rebuild(runtime, identifier), 202)
    assert runtime.run(runtime.worker.handle_event, event_for(database, task["processing_task_id"]))
    failed = descriptor(runtime, identifier)
    assert failed["ready"] and failed["artifacts"] == previous
    assert failed["rebuild_task"]["error_code"] == "PREVIEW_REBUILD_FAILED"
    assert "private detail" not in str(failed)
    assert indexed_snapshot(database, identifier) == before
    monkeypatch.setattr(service, "run_parser", original)
    retried = data(rebuild(runtime, identifier, key="retry-preview"), 202)
    assert runtime.run(
        runtime.worker.handle_event, event_for(database, retried["processing_task_id"])
    )
    assert descriptor(runtime, identifier)["rebuild_task"]["status"] == "success"
    assert indexed_snapshot(database, identifier) == before


def test_lost_preview_lease_cannot_replace_artifact_and_recovers(indexed, database):
    from enterprise_mind.ingestion.preview_rebuild import prepare_preview

    runtime = indexed[0]
    identifier = process(runtime, "docx", docx_bytes())["document_id"]
    previous = descriptor(runtime, identifier)["artifacts"]
    task = data(rebuild(runtime, identifier), 202)
    lease = runtime.run(runtime.worker.claim, int(task["processing_task_id"]))
    parsed = runtime.run(prepare_preview, runtime.worker, lease)
    with Session(database.engine) as session, session.begin():
        session.get(ProcessingUnit, lease.unit_id).lease_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    assert not runtime.run(runtime.worker.persist, lease, parsed)
    assert descriptor(runtime, identifier)["artifacts"] == previous
    assert runtime.run(runtime.dispatcher.recover) >= 1
    assert runtime.run(runtime.worker.handle_event, event_for(database, task["processing_task_id"]))
    assert descriptor(runtime, identifier)["rebuild_task"]["status"] == "success"


def test_recycled_during_rebuild_does_not_publish(indexed, database):
    from enterprise_mind.ingestion.preview_rebuild import prepare_preview

    runtime = indexed[0]
    identifier = process(runtime, "docx", docx_bytes())["document_id"]
    task = data(rebuild(runtime, identifier), 202)
    lease = runtime.run(runtime.worker.claim, int(task["processing_task_id"]))
    parsed = runtime.run(prepare_preview, runtime.worker, lease)
    with Session(database.engine) as session:
        version = session.get(Document, int(identifier)).row_version
        old_object = session.scalar(
            select(DocumentArtifact.storage_object_id).where(
                DocumentArtifact.artifact_type == "preview_html"
            )
        )
    pending = deletion(runtime, "documents", identifier, version)
    confirm(runtime, pending)
    assert runtime.run(runtime.worker.persist, lease, parsed)
    with Session(database.engine) as session:
        assert (
            session.get(ProcessingTask, int(task["processing_task_id"])).error_code
            == "PREVIEW_SOURCE_UNAVAILABLE"
        )
        assert (
            session.scalar(
                select(DocumentArtifact.storage_object_id).where(
                    DocumentArtifact.artifact_type == "preview_html"
                )
            )
            == old_object
        )


def test_rebuild_checks_auth_revision_and_version(indexed, database):
    runtime = indexed[0]
    identifier = process(runtime, "docx", docx_bytes())["document_id"]
    current = descriptor(runtime, identifier)
    body = {"row_version": current["row_version"], "revision_id": current["revision_id"]}
    url = f"/api/v1/documents/{identifier}/preview/rebuild"
    assert (
        runtime.client.post(url, headers={"Idempotency-Key": "no-login"}, json=body).status_code
        == 401
    )
    assert rebuild(runtime, identifier, body={**body, "row_version": 999}).status_code == 409
    assert rebuild(runtime, identifier, body={**body, "revision_id": "99999"}).status_code == 410
    assert data(rebuild(runtime, identifier, body=body), 202)["status"] == "pending"


def test_rebuild_rechecks_department_scope_before_idempotent_replay(indexed, database):
    runtime = indexed[0]
    identifier = process(runtime, "docx", docx_bytes())["document_id"]
    current = descriptor(runtime, identifier)
    body = {"row_version": current["row_version"], "revision_id": current["revision_id"]}
    with Session(database.engine) as session, session.begin():
        a, b = Department(name="A", name_key="a"), Department(name="B", name_key="b")
        session.add_all([a, b])
        session.flush()
        admin = session.get(User, database.user_id())
        session.add(
            User(
                username="重建成员",
                username_key="重建成员",
                role="user",
                department_id=a.id,
                password_hash=admin.password_hash,
            )
        )
        other_department = b.id
    login = runtime.client.post(
        "/api/v1/auth/login", json={"username": "重建成员", "password": database.admin_password}
    )
    headers = {
        "Authorization": "Bearer " + data(login)["access_token"],
        "Idempotency-Key": "scope-replay",
    }
    url = f"/api/v1/documents/{identifier}/preview/rebuild"
    assert runtime.client.post(url, headers=headers, json=body).status_code == 202
    with Session(database.engine) as session, session.begin():
        base = session.get(KnowledgeBase, int(runtime.base_id))
        base.type, base.department_id = "department", other_department
    assert runtime.client.post(url, headers=headers, json=body).status_code == 404
    assert (
        runtime.client.post(
            url, headers={**headers, "Idempotency-Key": "scope-new"}, json=body
        ).status_code
        == 404
    )


@pytest.mark.parametrize("missing_preview", [False, True])
def test_pdf_rebuild_reuses_original_without_scheduling_its_deletion(
    indexed, database, missing_preview
):
    import pymupdf

    runtime = indexed[0]
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((40, 40), "ABC-123 source")
        body = pdf.tobytes()
    identifier = process(runtime, "pdf", body)["document_id"]
    previous = descriptor(runtime, identifier)["artifacts"]
    if missing_preview:
        with Session(database.engine) as session, session.begin():
            session.execute(
                delete(DocumentArtifact).where(DocumentArtifact.artifact_type == "preview_pdf")
            )
        assert not descriptor(runtime, identifier)["ready"]
    task = data(rebuild(runtime, identifier), 202)
    assert runtime.run(runtime.worker.handle_event, event_for(database, task["processing_task_id"]))
    current = descriptor(runtime, identifier)
    assert current["ready"] and current["rebuild_task"]["status"] == "success"
    if not missing_preview:
        assert current["artifacts"] == previous
    assert (
        runtime.client.get(current["artifacts"][0]["content_url"], headers=runtime.admin).content
        == body
    )
    with Session(database.engine) as session:
        original = session.get(
            Revision, session.get(Document, int(identifier)).active_revision_id
        ).original_object_id
        assert session.get(StorageObject, original).status == "available"
        assert (
            session.scalar(
                select(OutboxEvent.id).where(OutboxEvent.event_type == "storage.delete.requested")
            )
            is None
        )
