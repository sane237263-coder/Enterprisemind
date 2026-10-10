# ruff: noqa: F811
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ingestion import ingestion_runtime  # noqa: F401
from test_lifecycle import confirm, data, deletion, folder, post
from test_rag_pipeline import indexed, upload_index  # noqa: F401

from enterprise_mind.agent.models import AnswerSource, Conversation, Message
from enterprise_mind.ingestion.cleanup import CleanupWorker
from enterprise_mind.ingestion.models import Document, OutboxEvent, Revision
from enterprise_mind.knowledge.models import DeletionBatch, KnowledgeBase
from enterprise_mind.rag.models import Chunk, ChunkAnchor

pytestmark = pytest.mark.integration


def test_retired_revision_cleanup_preserves_new_active_and_invalidates_source(indexed, database):
    runtime, *_ = indexed
    item = upload_index(indexed)
    with Session(database.engine) as session, session.begin():
        document = session.get(Document, int(item["document_id"]))
        old_id = document.active_revision_id
        old_object = session.get(Revision, old_id).original_object_id
        conversation = Conversation(owner_user_id=database.user_id(), title="citations")
        session.add(conversation)
        session.flush()
        message = Message(
            conversation_id=conversation.id,
            sequence=1,
            role="assistant",
            status="completed",
            content_text="历史答案。",
        )
        session.add(message)
        session.flush()
        anchor = session.scalar(
            select(ChunkAnchor).join(Chunk).where(Chunk.revision_id == old_id).limit(1)
        )
        source = AnswerSource(
            message_id=message.id,
            ordinal=1,
            chunk_id=anchor.chunk_id,
            anchor_id=anchor.anchor_id,
            source_title="旧来源",
            quote_text=anchor.quote_text[:100],
            locator_snapshot={"type": "text_range"},
        )
        session.add(source)
        session.flush()
        source_id = source.id
    action = data(
        runtime.client.post(
            f"/api/v1/documents/{item['document_id']}/overwrite-requests",
            headers={**runtime.admin, "Idempotency-Key": "retire"},
            files=[("files", ("next.txt", b"ABC-123 updated source", "text/plain"))],
            data={"row_version": "1"},
        ),
        202,
    )
    result = data(confirm(runtime, action))["result"]
    with Session(database.engine) as session:
        event = session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.deduplication_key
                == f"document.processing:{result['processing_task_id']}"
            )
        )
    assert runtime.run(runtime.worker.handle_event, event)
    cleanup = CleanupWorker(runtime.worker.sessions, runtime.worker.storage)
    with Session(database.engine) as session:
        retire = session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.event_type == "document.revision.cleanup_requested"
            )
        )
    assert not runtime.run(cleanup.handle, retire)
    with Session(database.engine) as session:
        storage_events = list(
            session.scalars(
                select(OutboxEvent.id).where(
                    OutboxEvent.event_type == "storage.delete.requested",
                )
            )
        )
        assert session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.event_type == "storage.delete.requested",
                OutboxEvent.aggregate_id == old_object,
            )
        )
    assert len(storage_events) == 2  # Original plus the generated preview.
    for storage_event in storage_events:
        assert runtime.run(cleanup.handle, storage_event)
    assert runtime.run(cleanup.handle, retire)
    with Session(database.engine) as session:
        assert session.get(Revision, old_id) is None
        document = session.get(Document, int(item["document_id"]))
        assert document.active_revision_id == int(result["revision_id"])
        source = session.get(AnswerSource, source_id)
        assert source.chunk_id is None and source.anchor_id is None and source.quote_text is None
        assert source.source_title == "旧来源"
    unavailable = runtime.client.get(
        f"/api/v1/answer-sources/{source_id}/preview", headers=runtime.admin
    )
    assert (
        unavailable.status_code == 410
        and unavailable.json()["error"]["code"] == "SOURCE_UNAVAILABLE"
    )


def test_purge_requires_batch_root_and_parent_purge_includes_independent_children(
    runtime, database
):
    root = folder(runtime, "root")
    independent = folder(runtime, "independent", root["id"])
    child = folder(runtime, "child", root["id"])
    deletion(runtime, "folders", independent["id"])
    data(confirm(runtime, deletion(runtime, "folders", root["id"])))
    invalid = post(runtime, f"folders/{child['id']}/purge-requests", {"row_version": 2})
    assert (
        invalid.status_code == 409 and invalid.json()["error"]["code"] == "RECYCLE_STATE_CONFLICT"
    )
    with Session(database.engine) as session:
        assert set(session.scalars(select(DeletionBatch.status))) == {"active"}
    action = data(post(runtime, f"folders/{root['id']}/purge-requests", {"row_version": 2}))
    assert data(confirm(runtime, action))["result"]["affected_count"] == 3
    with Session(database.engine) as session:
        event = session.scalar(
            select(OutboxEvent.id).where(OutboxEvent.event_type == "document.purge.requested")
        )
    cleanup = CleanupWorker(runtime.worker.sessions, runtime.worker.storage)
    assert runtime.run(cleanup.handle, event)
    with Session(database.engine) as session:
        assert list(session.scalars(select(DeletionBatch))) == []
        assert session.get(KnowledgeBase, int(runtime.base_id)) is not None


def test_retry_backfills_old_chunk_identity_without_collapsing_repeated_text(indexed, database):
    from enterprise_mind.ingestion.models import ProcessingTask, ProcessingUnit

    runtime, *_ = indexed
    item = upload_index(indexed, body="ABC-123 重复的原文内容。" * 150)
    with Session(database.engine) as session, session.begin():
        document = session.get(Document, int(item["document_id"]))
        old_ids = set(session.scalars(select(Chunk.id)))
        assert len(old_ids) >= 4
        for chunk in session.scalars(select(Chunk)):
            chunk.metadata_ = {
                key: value for key, value in chunk.metadata_.items() if key != "processing_identity"
            }
        unit = session.scalar(select(ProcessingUnit).where(ProcessingUnit.stage == "chunk"))
        unit.status = "failed"
        session.get(ProcessingTask, int(item["processing_task_id"])).status = "partial_success"
        document.processing_status = "partial_success"
        session.get(Revision, document.active_revision_id).processing_status = "partial_success"
    retry = data(
        post(runtime, f"documents/{item['document_id']}/processing/retry", {"row_version": 1}), 202
    )
    with Session(database.engine) as session:
        event = session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.deduplication_key
                == f"document.processing:{retry['processing_task_id']}"
            )
        )
    assert runtime.run(runtime.worker.handle_event, event)
    with Session(database.engine) as session:
        assert set(session.scalars(select(Chunk.id))) == old_ids
        assert all(
            "processing_identity" in chunk.metadata_ for chunk in session.scalars(select(Chunk))
        )
        assert session.get(Document, int(item["document_id"])).processing_status == "success"
