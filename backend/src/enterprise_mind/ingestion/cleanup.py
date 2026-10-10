"""Recoverable cleanup through the existing outbox; no external deletion in API transactions."""

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_mind.agent.models import AnswerSource
from enterprise_mind.identity.management import ORGANIZATION_LOCK
from enterprise_mind.ingestion.models import (
    Document,
    DocumentArtifact,
    Folder,
    OutboxEvent,
    ProcessingUnit,
    Revision,
    StorageObject,
)
from enterprise_mind.ingestion.storage import ObjectStorage
from enterprise_mind.knowledge import lifecycle as domain
from enterprise_mind.knowledge.models import DeletionBatch, KnowledgeBase
from enterprise_mind.knowledge.pending_models import PendingAction
from enterprise_mind.rag.models import Chunk

CLEANUP_EVENTS = (
    "document.purge.requested",
    "document.revision.cleanup_requested",
    "storage.delete.requested",
)


async def queue_storage_delete(session: AsyncSession, obj: StorageObject) -> None:
    if obj.status == "deleted":
        return
    obj.status = "pending_delete"
    key = f"storage.delete:{obj.id}"
    if (
        await session.scalar(select(OutboxEvent.id).where(OutboxEvent.deduplication_key == key))
        is None
    ):
        session.add(
            OutboxEvent(
                aggregate_type="storage_object",
                aggregate_id=obj.id,
                event_type="storage.delete.requested",
                payload={"storage_object_id": str(obj.id)},
                deduplication_key=key,
            )
        )


async def schedule_cleanup(
    sessions: async_sessionmaker[AsyncSession], redelivery_seconds: int, limit: int
) -> None:
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": ORGANIZATION_LOCK}
        )
        now = await session.scalar(select(domain.func_clock()))
        assert isinstance(now, datetime)
        await session.execute(
            update(PendingAction)
            .where(PendingAction.status == "pending", PendingAction.expires_at <= now)
            .values(status="expired", resolved_at=now)
        )
        objects = list(
            (
                await session.scalars(
                    select(StorageObject)
                    .where(StorageObject.status == "staged", StorageObject.retention_until <= now)
                    .order_by(StorageObject.id)
                    .limit(limit)
                    .with_for_update()
                )
            ).all()
        )
        for obj in objects:
            await queue_storage_delete(session, obj)
        batches = list(
            (
                await session.scalars(
                    select(DeletionBatch)
                    .where(DeletionBatch.status == "active", DeletionBatch.purge_after <= now)
                    .order_by(DeletionBatch.id)
                    .limit(limit)
                    .with_for_update()
                )
            ).all()
        )
        models = {"knowledge_base": KnowledgeBase, "folder": Folder, "document": Document}
        for batch in batches:
            root = await session.get(models[batch.root_type], batch.root_id)
            if not isinstance(root, KnowledgeBase | Folder | Document):
                continue
            rows = await domain.members(session, root, include_deleted=True)
            # A separately deleted child keeps its own retention window.
            if any(
                r.deleted_at is None or r.purge_after is None or r.purge_after > now for r in rows
            ):
                continue
            await domain.request_purge(session, root)
        await session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.event_type.in_(CLEANUP_EVENTS),
                OutboxEvent.status == "published",
                OutboxEvent.published_at <= now - timedelta(seconds=redelivery_seconds),
            )
            .values(status="pending", next_attempt_at=now, published_at=None)
        )


class CleanupWorker:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], storage: ObjectStorage) -> None:
        self.sessions, self.storage = sessions, storage

    async def handle(self, event_id: int) -> bool:
        async with self.sessions() as session:
            event = await session.get(OutboxEvent, event_id)
            if event is None:
                return True
            kind = event.event_type
        if kind == "storage.delete.requested":
            return await self.delete_storage(event_id)
        if kind not in CLEANUP_EVENTS:
            return False
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": ORGANIZATION_LOCK}
            )
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if event is None:
                return True
            if kind == "document.revision.cleanup_requested":
                document = await session.get(
                    Document, int(event.payload["document_id"]), with_for_update=True
                )
                revision = await session.get(
                    Revision, int(event.payload["revision_id"]), with_for_update=True
                )
                if revision is None:
                    await session.delete(event)
                    return True
                if (
                    document is None
                    or document.active_revision_id == revision.id
                    or revision.retired_at is None
                    or revision.revision_no == document.latest_revision_no
                ):
                    return False
                if not await self.delete_revisions(session, [revision]):
                    return False
                await session.delete(event)
                return True
            models = {"knowledge_base": KnowledgeBase, "folder": Folder, "document": Document}
            root = await session.get(
                models[event.payload["root_type"]],
                int(event.payload["root_id"]),
                with_for_update=True,
            )
            if not isinstance(root, KnowledgeBase | Folder | Document):
                await session.delete(event)
                return True
            rows = await domain.members(session, root, include_deleted=True)
            batch_ids = {int(i) for i in event.payload["batch_ids"]}
            if any(r.deleted_at is None or r.deletion_batch_id not in batch_ids for r in rows):
                return False
            docs = [r for r in rows if isinstance(r, Document)]
            revisions = list(
                (
                    await session.scalars(
                        select(Revision)
                        .where(Revision.document_id.in_([d.id for d in docs]))
                        .order_by(Revision.id)
                        .with_for_update()
                    )
                ).all()
            )
            failed_object = await session.scalar(
                select(StorageObject.id)
                .where(
                    or_(
                        StorageObject.id.in_([r.original_object_id for r in revisions]),
                        StorageObject.id.in_(
                            select(DocumentArtifact.storage_object_id).where(
                                DocumentArtifact.revision_id.in_([r.id for r in revisions])
                            )
                        ),
                    ),
                    StorageObject.status == "error",
                )
                .limit(1)
            )
            if failed_object:
                await session.execute(
                    update(DeletionBatch)
                    .where(DeletionBatch.id.in_(batch_ids))
                    .values(status="failed", failure_summary={"code": "STORAGE_DELETE_FAILED"})
                )
            if not await self.delete_revisions(session, revisions, docs):
                return False
            for document in docs:
                await session.delete(document)
            await session.flush()
            folders = {f.id: f for f in rows if isinstance(f, Folder)}
            while folders:
                parents = {f.parent_id for f in folders.values()}
                leaves = [f for f in folders.values() if f.id not in parents]
                if not leaves:
                    raise ValueError("invalid folder hierarchy")
                for folder in leaves:
                    await session.delete(folder)
                    del folders[folder.id]
                await session.flush()
            if isinstance(root, KnowledgeBase):
                await session.delete(root)
                await session.flush()
            for identifier in batch_ids:
                # A prior child event may already have removed its batch.
                batch = await session.get(DeletionBatch, identifier)
                if batch is not None:
                    await session.delete(batch)
            await session.delete(event)
            return True

    async def delete_revisions(
        self,
        session: AsyncSession,
        revisions: list[Revision],
        documents: list[Document] | None = None,
    ) -> bool:
        ids = [r.id for r in revisions]
        now = await session.scalar(select(domain.func_clock()))
        active = await session.scalar(
            select(ProcessingUnit.id)
            .where(
                ProcessingUnit.revision_id.in_(ids),
                ProcessingUnit.status == "processing",
                ProcessingUnit.lease_expires_at > now,
            )
            .limit(1)
        )
        if active is not None:
            return False
        objects = list(
            (
                await session.scalars(
                    select(StorageObject)
                    .where(
                        or_(
                            StorageObject.id.in_([r.original_object_id for r in revisions]),
                            StorageObject.id.in_(
                                select(DocumentArtifact.storage_object_id).where(
                                    DocumentArtifact.revision_id.in_(ids)
                                )
                            ),
                        )
                    )
                    .order_by(StorageObject.id)
                    .with_for_update()
                )
            ).all()
        )
        if any(obj.status != "deleted" for obj in objects):
            for obj in objects:
                await queue_storage_delete(session, obj)
            return False
        for document in documents or []:
            document.active_revision_id = None
        await session.flush()
        await session.execute(
            update(AnswerSource)
            .where(AnswerSource.chunk_id.in_(select(Chunk.id).where(Chunk.revision_id.in_(ids))))
            .values(quote_text=None)
        )
        await session.execute(delete(Revision).where(Revision.id.in_(ids)))
        await session.execute(
            delete(StorageObject).where(StorageObject.id.in_([o.id for o in objects]))
        )
        return True

    async def delete_storage(self, event_id: int) -> bool:
        async with self.sessions() as session, session.begin():
            event = await session.get(OutboxEvent, event_id)
            if event is None:
                return True
            obj = await session.get(StorageObject, event.aggregate_id, with_for_update=True)
            if obj is None or obj.status == "deleted":
                await session.delete(event)
                return True
            if (
                obj.status not in {"pending_delete", "deleting", "error"}
                or obj.bucket_name != self.storage.bucket
            ):
                return False
            identifier, key = obj.id, obj.object_key
            obj.status = "deleting"
        error = False
        try:
            await asyncio.to_thread(self.storage.remove, key)
        except Exception:
            error = True
        async with self.sessions() as session, session.begin():
            obj = await session.get(StorageObject, identifier, with_for_update=True)
            if obj is not None:
                obj.status = "error" if error else "deleted"
                obj.last_error = "对象清理暂时失败，等待重试。" if error else None
            if not error:
                await session.execute(delete(OutboxEvent).where(OutboxEvent.id == event_id))
                # Unreferenced staging objects need no aggregate cleanup.
                if (
                    obj
                    and await session.scalar(
                        select(Revision.id).where(Revision.original_object_id == identifier)
                    )
                    is None
                    and await session.scalar(
                        select(DocumentArtifact.id).where(
                            DocumentArtifact.storage_object_id == identifier
                        )
                    )
                    is None
                ):
                    await session.delete(obj)
            return not error
