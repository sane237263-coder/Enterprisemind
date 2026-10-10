"""Document lifecycle operations. Callers hold the organization write lock."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import rfc8785
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.agent.models import AgentRun, Conversation
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.identity.management import ORGANIZATION_LOCK, flush, knowledge_output
from enterprise_mind.identity.security import normalize_name, parse_id
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.models import (
    Document,
    Folder,
    OutboxEvent,
    ProcessingTask,
    ProcessingUnit,
    Revision,
    StorageObject,
)
from enterprise_mind.ingestion.service import document_output, processing_event, utc
from enterprise_mind.knowledge.models import DeletionBatch, KnowledgeBase

type Resource = KnowledgeBase | Folder | Document | Conversation


def digest(value: Any) -> str:
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


async def write_guard(session: AsyncSession, identity: CurrentIdentity) -> CurrentIdentity:
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ORGANIZATION_LOCK})
    return await AuthorizationService.revalidate(session, identity)


def accessible(identity: CurrentIdentity, base: KnowledgeBase) -> bool:
    AuthorizationService.require_active(identity)
    return (
        identity.user.role == "admin"
        or base.type == "public"
        or base.department_id == identity.user.department_id
    )


async def resource(
    session: AsyncSession,
    identity: CurrentIdentity,
    kind: str,
    identifier: int,
    *,
    recycled: bool | None = None,
    write: bool = True,
) -> Resource:
    models: dict[str, type[KnowledgeBase] | type[Folder] | type[Document] | type[Conversation]] = {
        "knowledge_base": KnowledgeBase,
        "folder": Folder,
        "document": Document,
        "conversation": Conversation,
    }
    model = models.get(kind)
    if model is None:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    query = select(model).where(model.id == identifier).execution_options(populate_existing=True)
    if write:
        query = query.with_for_update()
    row = await session.scalar(query)
    if row is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    assert isinstance(row, KnowledgeBase | Folder | Document | Conversation)
    if isinstance(row, Conversation):
        AuthorizationService.require_owner(identity, row.owner_user_id)
        return row
    base = (
        row
        if isinstance(row, KnowledgeBase)
        else await session.get(KnowledgeBase, row.knowledge_base_id, populate_existing=True)
    )
    if base is None or not accessible(identity, base):
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    if kind == "knowledge_base" and write:
        AuthorizationService.require_admin(identity)
    if recycled is not None and (row.deleted_at is not None) != recycled:
        raise APIError(ErrorCode.RECYCLE_STATE_CONFLICT if write else ErrorCode.RESOURCE_NOT_FOUND)
    if recycled is False and base.deleted_at is not None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    return row


def version(row: Resource, expected: int) -> None:
    if row.row_version != expected:
        raise APIError(ErrorCode.ROW_VERSION_CONFLICT)


def bump(row: Resource) -> None:
    row.row_version += 1
    row.updated_at = datetime.now(UTC)


def set_name(row: KnowledgeBase | Folder | Document, name: str) -> None:
    try:
        row.name_key = normalize_name(
            name, max_length=200 if isinstance(row, KnowledgeBase) else 255
        )
    except ValueError:
        raise APIError(ErrorCode.VALIDATION_ERROR) from None
    row.name = name.strip()


async def path_of(session: AsyncSession, row: Resource) -> str:
    if isinstance(row, Conversation):
        return row.title
    if isinstance(row, KnowledgeBase):
        return row.name
    parts = [row.name]
    parent = row.parent_id if isinstance(row, Folder) else row.folder_id
    seen: set[int] = set()
    while parent is not None:
        if parent in seen:
            raise APIError(ErrorCode.FOLDER_CYCLE)
        seen.add(parent)
        folder = await session.get(Folder, parent)
        if folder is None:
            break
        parts.append(folder.name)
        parent = folder.parent_id
    base = await session.get(KnowledgeBase, row.knowledge_base_id)
    if base:
        parts.append(base.name)
    return "/" + "/".join(reversed(parts))


async def output(session: AsyncSession, row: Resource) -> dict[str, Any]:
    if isinstance(row, Conversation):
        return {"id": str(row.id), "title": row.title, "row_version": row.row_version}
    if isinstance(row, Document):
        result = await document_output(session, row)
    elif isinstance(row, KnowledgeBase):
        result = knowledge_output(row).model_dump(mode="json")
    else:
        result = {
            "id": str(row.id),
            "knowledge_base_id": str(row.knowledge_base_id),
            "parent_id": str(row.parent_id) if row.parent_id else None,
            "name": row.name,
            "row_version": row.row_version,
        }
    result.update(
        path=await path_of(session, row),
        scope="recycle_bin" if row.deleted_at else "active",
        deleted_at=utc(row.deleted_at),
        purge_after=utc(row.purge_after),
        deletion_batch_id=str(row.deletion_batch_id) if row.deletion_batch_id else None,
    )
    batch = (
        await session.get(DeletionBatch, row.deletion_batch_id) if row.deletion_batch_id else None
    )
    result["is_deletion_root"] = bool(
        batch and batch.root_type == kind_of(row) and batch.root_id == row.id
    )
    return result


async def members(
    session: AsyncSession, root: Resource, *, include_deleted: bool = False
) -> list[KnowledgeBase | Folder | Document]:
    if isinstance(root, Conversation):
        return []
    if isinstance(root, Document):
        return [root]
    base_id = root.id if isinstance(root, KnowledgeBase) else root.knowledge_base_id
    folders = list(
        (
            await session.scalars(
                select(Folder)
                .where(Folder.knowledge_base_id == base_id)
                .order_by(Folder.id)
                .with_for_update()
            )
        ).all()
    )
    included = {root.id} if isinstance(root, Folder) else {f.id for f in folders}
    if isinstance(root, Folder):
        while True:
            more = {f.id for f in folders if f.parent_id in included}
            if more <= included:
                break
            included |= more
    docs_query = select(Document).where(Document.knowledge_base_id == base_id)
    if isinstance(root, Folder):
        docs_query = docs_query.where(Document.folder_id.in_(included))
    docs = list((await session.scalars(docs_query.order_by(Document.id).with_for_update())).all())
    rows: list[KnowledgeBase | Folder | Document] = (
        [root] if isinstance(root, KnowledgeBase) else []
    )
    rows.extend(f for f in folders if f.id in included)
    rows.extend(docs)
    return [r for r in rows if include_deleted or r.deleted_at is None]


def kind_of(row: Resource) -> str:
    return {
        KnowledgeBase: "knowledge_base",
        Folder: "folder",
        Document: "document",
        Conversation: "conversation",
    }[type(row)]


async def snapshot(session: AsyncSession, root: Resource, operation: str) -> dict[str, Any]:
    if operation.endswith("purge") and not isinstance(root, Conversation):
        batch = await session.get(DeletionBatch, root.deletion_batch_id, with_for_update=True)
        if batch is None or batch.root_id != root.id or batch.root_type != kind_of(root):
            raise APIError(ErrorCode.RECYCLE_STATE_CONFLICT)
    result: dict[str, Any] = {
        "resource_type": kind_of(root),
        "id": str(root.id),
        "name": root.title if isinstance(root, Conversation) else root.name,
        "path": await path_of(session, root),
        "row_version": str(root.row_version),
    }
    if isinstance(root, Conversation):
        result["next_message_sequence"] = str(root.next_message_sequence)
        return result
    rows = await members(session, root, include_deleted=operation.endswith("purge"))
    items = sorted(
        [
            [
                kind_of(r),
                str(r.id),
                str(r.row_version),
                str(r.deletion_batch_id) if r.deletion_batch_id else None,
            ]
            for r in rows
        ]
    )
    result.update(
        member_count=len(items),
        member_hash=digest(items),
        deletion_batch_id=str(root.deletion_batch_id) if root.deletion_batch_id else None,
    )
    if operation.endswith("purge"):
        batch_ids = {r.deletion_batch_id for r in rows if r.deletion_batch_id}
        batches = list(
            (
                await session.scalars(
                    select(DeletionBatch)
                    .where(DeletionBatch.id.in_(batch_ids))
                    .order_by(DeletionBatch.id)
                    .with_for_update()
                )
            ).all()
        )
        result["batches"] = [[str(b.id), b.status] for b in batches]
    return result


async def parent_scope(
    session: AsyncSession,
    identity: CurrentIdentity,
    base_id: int,
    parent_id: int | None,
    moving: Folder | None = None,
) -> None:
    await resource(session, identity, "knowledge_base", base_id, recycled=False, write=False)
    if parent_id is None:
        return
    parent = await resource(session, identity, "folder", parent_id, recycled=False)
    assert isinstance(parent, Folder)
    if parent.knowledge_base_id != base_id:
        raise APIError(ErrorCode.CROSS_KNOWLEDGE_BASE_MOVE_FORBIDDEN)
    seen = {moving.id} if moving else set()
    while parent:
        if parent.id in seen:
            raise APIError(ErrorCode.FOLDER_CYCLE)
        seen.add(parent.id)
        if parent.parent_id is None:
            break
        ancestor = await session.get(Folder, parent.parent_id)
        if ancestor is None or ancestor.deleted_at is not None:
            raise APIError(ErrorCode.RECYCLE_STATE_CONFLICT)
        parent = ancestor


async def create_folder(
    session: AsyncSession, identity: CurrentIdentity, base_id: int, values: dict[str, Any]
) -> dict[str, Any]:
    parent_id = parse_id(values["parent_id"]) if values.get("parent_id") else None
    await parent_scope(session, identity, base_id, parent_id)
    folder = Folder(knowledge_base_id=base_id, parent_id=parent_id)
    set_name(folder, values["name"])
    session.add(folder)
    await flush(session)
    return await output(session, folder)


async def update_resource(
    session: AsyncSession,
    identity: CurrentIdentity,
    kind: str,
    identifier: int,
    values: dict[str, Any],
) -> dict[str, Any]:
    row = await resource(session, identity, kind, identifier, recycled=False)
    assert isinstance(row, Folder | Document)
    version(row, values["row_version"])
    if values.get("name") is not None:
        set_name(row, values["name"])
    field = "parent_id" if isinstance(row, Folder) else "folder_id"
    if field in values:
        parent_id = parse_id(values[field]) if values[field] else None
        await parent_scope(
            session,
            identity,
            row.knowledge_base_id,
            parent_id,
            row if isinstance(row, Folder) else None,
        )
        setattr(row, field, parent_id)
    if isinstance(row, Document) and "description" in values:
        row.description = values["description"]
    bump(row)
    await flush(session)
    return await output(session, row)


async def soft_delete(session: AsyncSession, root: Resource) -> dict[str, Any]:
    assert not isinstance(root, Conversation)
    rows = await members(session, root)
    now = await session.scalar(select(func_clock()))
    assert isinstance(now, datetime)
    batch = DeletionBatch(
        root_type=kind_of(root),
        root_id=root.id,
        root_name=root.name,
        deleted_at=now,
        purge_after=now + timedelta(days=30),
        status="active",
    )
    session.add(batch)
    await session.flush()
    for row in rows:
        row.deleted_at, row.purge_after, row.deletion_batch_id = (
            batch.deleted_at,
            batch.purge_after,
            batch.id,
        )
        bump(row)
    await session.flush()
    return {
        "id": str(root.id),
        "status": "recycle_bin",
        "deletion_batch_id": str(batch.id),
        "affected_count": len(rows),
        "purge_after": utc(batch.purge_after),
    }


def func_clock() -> Any:
    # Statement time, unlike transaction start time, remains correct after lock waits.
    from sqlalchemy import func

    return func.clock_timestamp()


async def restore(
    session: AsyncSession,
    identity: CurrentIdentity,
    kind: str,
    identifier: int,
    values: dict[str, Any],
) -> dict[str, Any]:
    root = await resource(session, identity, kind, identifier, recycled=True)
    assert not isinstance(root, Conversation)
    version(root, values["row_version"])
    batch = await session.get(DeletionBatch, root.deletion_batch_id, with_for_update=True)
    now = await session.scalar(select(func_clock()))
    assert isinstance(now, datetime)
    if (
        batch is None
        or batch.status != "active"
        or batch.purge_after <= now
        or batch.root_id != root.id
        or batch.root_type != kind
    ):
        raise APIError(ErrorCode.RECYCLE_STATE_CONFLICT)
    if values.get("name"):
        set_name(root, values["name"])
    if isinstance(root, Folder | Document):
        old_parent = root.parent_id if isinstance(root, Folder) else root.folder_id
        parent = (
            (parse_id(values["parent_id"]) if values["parent_id"] else None)
            if "parent_id" in values
            else old_parent
        )
        await parent_scope(
            session,
            identity,
            root.knowledge_base_id,
            parent,
            root if isinstance(root, Folder) else None,
        )
        if isinstance(root, Folder):
            root.parent_id = parent
        else:
            root.folder_id = parent
    rows = [
        r
        for r in await members(session, root, include_deleted=True)
        if r.deletion_batch_id == batch.id
    ]
    for row in rows:
        row.deleted_at = row.purge_after = row.deletion_batch_id = None
        bump(row)
    await flush(session)
    # Activation waiting in the recycle bin is delivered again after the restore commits.
    for row in rows:
        if isinstance(row, Document):
            tasks = list(
                (
                    await session.scalars(
                        select(ProcessingTask)
                        .join(Revision)
                        .where(
                            Revision.document_id == row.id,
                            ProcessingTask.status.in_(["pending", "processing"]),
                        )
                    )
                ).all()
            )
            for task in tasks:
                event = processing_event(row.id, task.revision_id, task.id)
                event.deduplication_key = f"document.restore:{batch.id}:{task.id}"
                session.add(event)
    await session.delete(batch)
    await session.flush()
    return await output(session, root)


async def retry_processing(session: AsyncSession, document: Document) -> dict[str, Any]:
    revision = await session.scalar(
        select(Revision)
        .where(
            Revision.document_id == document.id, Revision.revision_no == document.latest_revision_no
        )
        .with_for_update()
    )
    assert revision is not None
    previous = await session.scalar(
        select(ProcessingTask)
        .where(
            ProcessingTask.revision_id == revision.id, ProcessingTask.task_type != "preview_rebuild"
        )
        .order_by(ProcessingTask.id.desc())
        .limit(1)
        .with_for_update()
    )
    units = list(
        (
            await session.scalars(
                select(ProcessingUnit)
                .where(
                    ProcessingUnit.revision_id == revision.id,
                    ~((ProcessingUnit.stage == "preview") & (ProcessingUnit.unit_key == "rebuild")),
                )
                .order_by(ProcessingUnit.id)
                .with_for_update()
            )
        ).all()
    )
    failed = [u for u in units if u.status == "failed"]
    busy = await session.scalar(
        select(ProcessingTask.id)
        .where(
            ProcessingTask.revision_id == revision.id,
            ProcessingTask.status.in_(("pending", "processing")),
        )
        .limit(1)
    )
    if previous is None or busy or not failed:
        raise APIError(ErrorCode.PROCESSING_NOT_RETRYABLE)
    task = ProcessingTask(
        revision_id=revision.id,
        task_type="retry_failed",
        retry_of_task_id=previous.id,
        current_stage=failed[0].stage,
        stage_current=0,
        stage_total=len(failed),
    )
    session.add(task)
    await session.flush()
    # Successful source units and embeddings stay usable during a partial retry.
    failed_sources = {
        u.source_unit_id for u in failed if u.stage in {"ocr", "vision", "table", "code"}
    }
    for unit in units:
        dependent = unit.stage == "activate" or (
            unit.stage == "chunk" and unit.source_unit_id in failed_sources
        )
        if unit.status == "failed" or dependent:
            unit.status, unit.latest_task_id = "pending", task.id
            unit.current = 0
            unit.lease_owner = unit.lease_expires_at = None
            unit.error_code = unit.error_message = None
            unit.started_at = unit.finished_at = None
    if revision.processing_status != "partial_success":
        document.processing_status = revision.processing_status = "processing"
    bump(document)
    session.add(processing_event(document.id, revision.id, task.id))
    await session.flush()
    return {
        "id": str(document.id),
        "processing_task_id": str(task.id),
        "retry_of_task_id": str(previous.id),
        "status": "pending",
    }


async def overwrite(
    session: AsyncSession, identity: CurrentIdentity, document: Document, arguments: dict[str, Any]
) -> dict[str, Any]:
    staged = await session.get(
        StorageObject, parse_id(arguments["staged_upload_id"]), with_for_update=True
    )
    now = await session.scalar(select(func_clock()))
    assert isinstance(now, datetime)
    if (
        staged is None
        or staged.status != "staged"
        or staged.content_sha256 != arguments["content_sha256"]
        or staged.retention_until is None
        or staged.retention_until <= now
    ):
        raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
    active_task = await session.scalar(
        select(ProcessingTask.id)
        .join(Revision)
        .where(
            Revision.document_id == document.id,
            ProcessingTask.status.in_(["pending", "processing"]),
        )
        .limit(1)
    )
    if active_task is not None:
        raise APIError(ErrorCode.DOCUMENT_PROCESSING_IN_PROGRESS)
    document.latest_revision_no += 1
    document.processing_status = "pending"
    bump(document)
    staged.status, staged.retention_until = "available", None
    revision = Revision(
        document_id=document.id,
        revision_no=document.latest_revision_no,
        trigger_type="overwrite",
        original_object_id=staged.id,
        original_file_name=arguments["file_name"],
        file_extension=arguments["file_extension"],
        mime_type=staged.content_type,
        size_bytes=staged.size_bytes,
        content_sha256=staged.content_sha256,
        initiated_by=identity.user.id,
    )
    session.add(revision)
    await session.flush()
    task = ProcessingTask(
        revision_id=revision.id,
        task_type="full",
        current_stage="parse",
        stage_current=0,
        stage_total=1,
    )
    session.add(task)
    await session.flush()
    session.add(
        ProcessingUnit(
            revision_id=revision.id, stage="parse", unit_key="document", latest_task_id=task.id
        )
    )
    session.add(processing_event(document.id, revision.id, task.id))
    await session.flush()
    return {
        "id": str(document.id),
        "revision_id": str(revision.id),
        "processing_task_id": str(task.id),
        "status": "pending",
    }


async def request_purge(session: AsyncSession, root: Resource) -> dict[str, Any]:
    assert not isinstance(root, Conversation)
    batch = await session.get(DeletionBatch, root.deletion_batch_id, with_for_update=True)
    if batch is None or batch.root_id != root.id or batch.root_type != kind_of(root):
        raise APIError(ErrorCode.RECYCLE_STATE_CONFLICT)
    rows = await members(session, root, include_deleted=True)
    if any(r.deleted_at is None for r in rows):
        raise APIError(ErrorCode.RECYCLE_STATE_CONFLICT)
    batch_ids = sorted({r.deletion_batch_id for r in rows if r.deletion_batch_id})
    for identifier in batch_ids:
        batch = await session.get(DeletionBatch, identifier, with_for_update=True)
        if batch is not None:
            batch.status, batch.failure_summary = "purging", None
    event = OutboxEvent(
        aggregate_type="deletion_batch",
        aggregate_id=root.deletion_batch_id,
        event_type="document.purge.requested",
        payload={
            "root_type": kind_of(root),
            "root_id": str(root.id),
            "batch_ids": [str(i) for i in batch_ids],
        },
        deduplication_key=f"purge:{root.deletion_batch_id}",
    )
    existing = await session.scalar(
        select(OutboxEvent).where(OutboxEvent.deduplication_key == event.deduplication_key)
    )
    if existing is None:
        session.add(event)
    else:
        existing.status, existing.next_attempt_at = "pending", datetime.now(UTC)
    await session.flush()
    return {"id": str(root.id), "status": "purging", "affected_count": len(rows)}


async def apply_operation(
    session: AsyncSession,
    identity: CurrentIdentity,
    operation: str,
    root: Resource,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if operation == "conversation.delete":
        assert isinstance(root, Conversation)
        runs = list(
            (
                await session.scalars(
                    select(AgentRun)
                    .where(AgentRun.conversation_id == root.id)
                    .order_by(AgentRun.id)
                    .with_for_update()
                )
            ).all()
        )
        if any(run.status in {"pending", "running", "waiting_confirmation"} for run in runs):
            raise APIError(ErrorCode.RUN_ALREADY_ACTIVE)
        from enterprise_mind.ingestion.cleanup import queue_storage_delete

        staged = await session.scalars(
            select(StorageObject)
            .where(
                StorageObject.attachment_conversation_id == root.id,
                StorageObject.status == "staged",
            )
            .with_for_update()
        )
        for obj in staged:
            await queue_storage_delete(session, obj)
        session.add(
            OutboxEvent(
                aggregate_type="conversation",
                aggregate_id=root.id,
                event_type="conversation.checkpoints_cleanup.requested",
                payload={"run_ids": [str(run.id) for run in runs]},
                deduplication_key=f"conversation.cleanup:{root.id}",
            )
        )
        result = {"id": str(root.id), "status": "deleted"}
        await session.delete(root)
        await session.flush()
        return result
    if operation.endswith(".delete"):
        return await soft_delete(session, root)
    if operation.endswith(".purge"):
        return await request_purge(session, root)
    if operation == "document.overwrite":
        assert isinstance(root, Document)
        return await overwrite(session, identity, root, arguments)
    raise APIError(ErrorCode.VALIDATION_ERROR)
