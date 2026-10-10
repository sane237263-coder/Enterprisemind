# ruff: noqa: F811
from datetime import UTC, datetime, timedelta

import pytest
from media_samples import docx_bytes, image_bytes, pptx_bytes, xlsx_bytes
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_ingestion import accepted, ingestion_runtime  # noqa: F401
from test_lifecycle import confirm, data, deletion, post
from test_rag_pipeline import indexed, retrieve  # noqa: F401

from enterprise_mind.identity.models import Department, User
from enterprise_mind.ingestion.cleanup import CleanupWorker
from enterprise_mind.ingestion.media import MediaService
from enterprise_mind.ingestion.media_providers import OCRResult, VisionResult
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
from enterprise_mind.ingestion.uploads import MIMES
from enterprise_mind.ingestion.worker import IngestionWorker
from enterprise_mind.knowledge.models import KnowledgeBase
from enterprise_mind.rag.models import Chunk
from enterprise_mind.rag.providers import ProviderFailure

pytestmark = pytest.mark.integration


class FakeOCR:
    model_key = "test-ocr-v1"
    calls = 0

    async def recognize(self, image, width, height):
        self.calls += 1
        return OCRResult.model_validate(
            {
                "blocks": [
                    {
                        "text": "ABC-123 扫描文本",
                        "box": {"x": 8.0, "y": 10.0, "width": 90.0, "height": 20.0},
                        "confidence": 0.98,
                    }
                ]
            }
        )


class FakeVision:
    model_key = "test-vision-v1"
    calls = 0
    fail = False

    async def describe(self, image, width, height):
        self.calls += 1
        if self.fail:
            raise ProviderFailure("MEDIA_PROVIDER_UNAVAILABLE")
        return VisionResult(
            description="图片语义：ABC-123 设备示意图。标签较模糊。",
            image_type="diagram",
            uncertainties=["标签较模糊"],
        )


@pytest.fixture
def media_indexed(indexed):
    runtime = indexed[0]
    old = runtime.worker
    ocr, vision = FakeOCR(), FakeVision()
    media = MediaService(old.sessions, old.storage, old.config, ocr, vision)
    runtime.worker = IngestionWorker(
        old.sessions, old.storage, old.queue, old.config, old.indexing, media
    )
    return indexed, ocr, vision


def process(runtime, extension, body):
    item = accepted(runtime.upload([("files", ("sample." + extension, body, MIMES[extension]))]))
    runtime.run(runtime.dispatcher.dispatch_once)
    assert runtime.run(runtime.worker.run_once)
    return item


@pytest.mark.parametrize(
    "extension,builder", [("docx", docx_bytes), ("pptx", pptx_bytes), ("xlsx", xlsx_bytes)]
)
def test_office_upload_index_preview_download_and_range(indexed, database, extension, builder):
    runtime = indexed[0]
    body = builder()
    item = process(runtime, extension, body)
    document_id = item["document_id"]
    with Session(database.engine) as session:
        document = session.get(Document, int(document_id))
        assert document.processing_status == "success"
        assert session.scalar(select(func.count()).select_from(DocumentArtifact)) == 1
        anchor = session.scalar(select(SourceAnchor).order_by(SourceAnchor.id))
        anchor_id = anchor.id
    assert retrieve(indexed).contexts
    preview = runtime.client.get(
        f"/api/v1/documents/{document_id}/preview?anchor_id={anchor_id}", headers=runtime.admin
    )
    assert preview.status_code == 200, preview.text
    payload = preview.json()["data"]
    assert payload["ready"] and payload["highlight"]["locator_type"] == extension
    for forbidden in ("object_key", "original/", "bucket_name", "password"):
        assert forbidden not in preview.text
    content_url = payload["artifacts"][0]["content_url"]
    rendered = runtime.client.get(content_url, headers=runtime.admin)
    assert rendered.status_code == 200 and "ABC-123" in rendered.text
    assert (
        "<script>" not in rendered.text and "sandbox" in rendered.headers["content-security-policy"]
    )
    assert runtime.client.get(content_url).status_code == 401
    downloaded = runtime.client.get(payload["download_url"], headers=runtime.admin)
    assert downloaded.content == body
    portion = runtime.client.get(
        payload["download_url"], headers={**runtime.admin, "Range": "bytes=0-3"}
    )
    assert portion.status_code == 206 and portion.content == b"PK\x03\x04"


def test_image_ocr_vision_citations_and_selective_retry(media_indexed, database):
    indexed, ocr, vision = media_indexed
    runtime = indexed[0]
    vision.fail = True
    item = process(runtime, "png", image_bytes())
    document_id = int(item["document_id"])
    with Session(database.engine) as session:
        document = session.get(Document, document_id)
        assert document.processing_status == "partial_success"
        assert document.active_revision_id is not None
        source = session.scalars(select(SourceUnit)).one()
        assert source.extraction_status == "success"
        assert (
            session.scalar(select(ProcessingUnit.status).where(ProcessingUnit.stage == "vision"))
            == "failed"
        )
        assert session.scalar(select(func.count()).select_from(DocumentArtifact)) == 3
        anchor = session.scalar(
            select(SourceAnchor).where(SourceAnchor.source_text == "ABC-123 扫描文本")
        )
        assert anchor.locator["boxes"][0] == {"x": 8.0, "y": 10.0, "width": 90.0, "height": 20.0}
    assert retrieve(indexed).contexts and ocr.calls == 1
    vision.fail = False
    response = data(
        post(runtime, f"documents/{document_id}/processing/retry", {"row_version": 1}), 202
    )
    with Session(database.engine) as session:
        assert session.get(Document, document_id).processing_status == "partial_success"
        event = session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.deduplication_key
                == "document.processing:" + response["processing_task_id"]
            )
        )
    assert runtime.run(runtime.worker.handle_event, event)
    assert ocr.calls == 1 and vision.calls == 2
    with Session(database.engine) as session:
        assert session.get(Document, document_id).processing_status == "success"
        element = session.scalar(
            select(ContentElement).where(ContentElement.element_type == "image")
        )
        assert element.structured_payload["text_origin"] == "vision"
        anchor_id = session.scalar(
            select(SourceAnchor.id).where(SourceAnchor.element_id == element.id)
        )
        assert session.scalar(select(func.count()).select_from(DocumentArtifact)) == 4
    preview = runtime.client.get(
        f"/api/v1/documents/{document_id}/preview?anchor_id={anchor_id}", headers=runtime.admin
    )
    assert preview.json()["data"]["highlight"]["text_origin"] == "vision"


def test_unconfigured_media_waits_without_fake_success(indexed, database):
    item = process(indexed[0], "png", image_bytes())
    with Session(database.engine) as session:
        document = session.get(Document, int(item["document_id"]))
        assert document.processing_status == "processing" and document.active_revision_id is None
        assert set(
            session.scalars(
                select(ProcessingUnit.status).where(ProcessingUnit.stage.in_(("ocr", "vision")))
            )
        ) == {"pending"}
        assert not session.scalar(select(Chunk.id))
    assert not retrieve(indexed).contexts


def test_ppt_embedded_image_preview_exposes_image_bounds(media_indexed, database):
    indexed, _, _ = media_indexed
    runtime = indexed[0]
    item = process(runtime, "pptx", pptx_bytes(pictures=True))
    with Session(database.engine) as session:
        source = session.scalars(select(SourceUnit).where(SourceUnit.unit_type == "image")).one()
        bounds = source.metadata_["origin_locator"]["boxes"][0]
        anchor = session.scalar(
            select(SourceAnchor).where(
                SourceAnchor.source_unit_id == source.id,
                SourceAnchor.source_text == "ABC-123 扫描文本",
            )
        )
        anchor_id = anchor.id
    preview = runtime.client.get(
        f"/api/v1/documents/{item['document_id']}/preview?anchor_id={anchor_id}",
        headers=runtime.admin,
    )
    highlight = data(preview)["highlight"]
    assert highlight["image_bounds"] == bounds
    assert bounds["x"] == 72 and bounds["y"] == 144
    assert bounds["width"] < highlight["width"]
    assert highlight["locator"]["boxes"][0]["x"] > bounds["x"]


def test_late_media_results_do_not_commit_and_lease_recovery_is_idempotent(media_indexed, database):
    indexed, _, _ = media_indexed
    runtime = indexed[0]
    item = accepted(runtime.upload([("files", ("image.png", image_bytes(), "image/png"))]))
    task_id = int(item["processing_task_id"])
    parse_lease = runtime.run(runtime.worker.claim, task_id)
    assert runtime.run(runtime.worker.execute, parse_lease)
    lease = runtime.run(runtime.worker.claim, task_id)
    assert lease.stage == "ocr"
    prepared = runtime.run(runtime.worker.media.prepare, lease)
    assert prepared.error is None
    with Session(database.engine) as session, session.begin():
        session.get(ProcessingUnit, lease.unit_id).lease_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )

    async def late():
        return await runtime.worker.persist(lease, None, media=prepared)

    assert not runtime.run(late)
    with Session(database.engine) as session:
        assert not session.scalar(select(SourceAnchor.id))
    runtime.run(runtime.dispatcher.recover)
    with Session(database.engine) as session:
        event = session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.deduplication_key == f"document.processing:{task_id}"
            )
        )
    assert runtime.run(runtime.worker.handle_event, event)
    assert runtime.run(runtime.worker.handle_event, event)
    with Session(database.engine) as session:
        assert session.get(Document, int(item["document_id"])).processing_status == "success"
        assert session.scalar(select(func.count()).select_from(DocumentArtifact)) == 4


def test_preview_recycle_revocation_and_purge_removes_all_artifacts(indexed, database):
    runtime = indexed[0]
    item = process(runtime, "docx", docx_bytes())
    document_id = item["document_id"]
    payload = runtime.client.get(
        f"/api/v1/documents/{document_id}/preview", headers=runtime.admin
    ).json()["data"]
    url = payload["artifacts"][0]["content_url"]
    data(confirm(runtime, deletion(runtime, "documents", document_id)))
    assert runtime.client.get(url, headers=runtime.admin).status_code == 404
    action = data(post(runtime, f"documents/{document_id}/purge-requests", {"row_version": 2}))
    data(confirm(runtime, action))
    cleanup = CleanupWorker(runtime.worker.sessions, runtime.worker.storage)
    with Session(database.engine) as session:
        event = session.scalar(
            select(OutboxEvent.id).where(OutboxEvent.event_type == "document.purge.requested")
        )
        objects = list(session.scalars(select(StorageObject.object_key)))
    assert not runtime.run(cleanup.handle, event)
    with Session(database.engine) as session:
        storage_events = list(
            session.scalars(
                select(OutboxEvent.id).where(OutboxEvent.event_type == "storage.delete.requested")
            )
        )
    for identifier in storage_events:
        assert runtime.run(cleanup.handle, identifier)
    assert runtime.run(cleanup.handle, event)
    with Session(database.engine) as session:
        assert not session.scalar(select(DocumentArtifact.id)) and not session.scalar(
            select(StorageObject.id)
        )
    assert len(objects) == 2


def test_legacy_without_converter_is_explicit_and_retryable(indexed, database):
    runtime = indexed[0]
    item = process(runtime, "doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    with Session(database.engine) as session:
        assert session.get(Document, int(item["document_id"])).processing_status == "failed"
        task = session.scalars(select(ProcessingTask)).one()
        assert task.error_code == "OFFICE_CONVERTER_NOT_CONFIGURED"
        assert session.scalars(select(Revision)).one().file_extension == "doc"


def test_preview_rechecks_credentials_after_storage_download(indexed, database, monkeypatch):
    runtime = indexed[0]
    item = process(runtime, "docx", docx_bytes())
    preview = runtime.client.get(
        f"/api/v1/documents/{item['document_id']}/preview", headers=runtime.admin
    )
    url = preview.json()["data"]["artifacts"][0]["content_url"]
    original = runtime.worker.storage.download

    def revoke_after_transfer(*args):
        original(*args)
        with Session(database.engine) as session, session.begin():
            session.get(User, database.user_id()).credential_version += 1

    monkeypatch.setattr(runtime.worker.storage, "download", revoke_after_transfer)
    assert runtime.client.get(url, headers=runtime.admin).status_code == 401


def test_same_preview_url_respects_changed_department_scope(indexed, database):
    runtime = indexed[0]
    item = process(runtime, "docx", docx_bytes())
    preview = runtime.client.get(
        f"/api/v1/documents/{item['document_id']}/preview", headers=runtime.admin
    )
    url = preview.json()["data"]["artifacts"][0]["content_url"]
    with Session(database.engine) as session, session.begin():
        a, b = Department(name="A", name_key="a"), Department(name="B", name_key="b")
        session.add_all([a, b])
        session.flush()
        admin = session.get(User, database.user_id())
        user = User(
            username="预览用户",
            username_key="预览用户",
            role="user",
            department_id=a.id,
            password_hash=admin.password_hash,
        )
        session.add(user)
        second_department = b.id
    login = runtime.client.post(
        "/api/v1/auth/login", json={"username": "预览用户", "password": database.admin_password}
    )
    headers = {"Authorization": "Bearer " + login.json()["data"]["access_token"]}
    assert runtime.client.get(url, headers=headers).status_code == 200
    with Session(database.engine) as session, session.begin():
        base = session.get(KnowledgeBase, int(runtime.base_id))
        base.type, base.department_id = "department", second_department
    assert runtime.client.get(url, headers=headers).status_code == 404


def test_pending_old_pdf_generates_missing_page_artifact(media_indexed, database):
    import pymupdf

    indexed, _, _ = media_indexed
    runtime = indexed[0]
    with pymupdf.open() as document:
        document.new_page(width=300, height=200)
        body = document.tobytes()
    item = accepted(runtime.upload([("files", ("old.pdf", body, "application/pdf"))]))
    task_id = int(item["processing_task_id"])
    lease = runtime.run(runtime.worker.claim, task_id)
    assert runtime.run(runtime.worker.execute, lease)
    with Session(database.engine) as session, session.begin():
        # Simulate the source records written by pre-media parsing.
        artifact = session.scalar(
            select(DocumentArtifact).where(DocumentArtifact.artifact_type == "page_image")
        )
        session.delete(artifact)
        source = session.scalars(select(SourceUnit)).one()
        source.metadata_ = {"needs_ocr": True, "needs_vision": False, "rotation": 0}
    lease = runtime.run(runtime.worker.claim, task_id)
    assert lease.stage == "ocr" and runtime.run(runtime.worker.execute, lease)
    with Session(database.engine) as session:
        assert session.scalar(
            select(DocumentArtifact.id).where(DocumentArtifact.artifact_type == "page_image")
        )
        anchor = session.scalar(select(SourceAnchor))
        assert anchor.locator["page"] == 1
        assert anchor.locator["boxes"][0]["width"] == 45.0  # 2x rendered pixels -> PDF points.
