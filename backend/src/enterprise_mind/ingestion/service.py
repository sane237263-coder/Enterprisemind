import hmac
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.api.errors import ERRORS, APIError, ErrorCode
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.identity.management import ORGANIZATION_LOCK, get_knowledge_base
from enterprise_mind.identity.security import Tokens, normalize_name, parse_id
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.infrastructure.models import IdempotencyRecord
from enterprise_mind.ingestion.models import (
    Document,
    Folder,
    OutboxEvent,
    ProcessingTask,
    ProcessingUnit,
    Revision,
    StorageObject,
)
from enterprise_mind.ingestion.uploads import UploadedFile
from enterprise_mind.knowledge.models import KnowledgeBase


async def visible_document(
    session: AsyncSession, identity: CurrentIdentity, document_id: int, *, lock: bool = False
) -> Document:
    query = (
        select(Document)
        .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
        .where(
            Document.id == document_id,
            Document.deleted_at.is_(None),
            AuthorizationService.knowledge_scope(identity),
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update(of=Document)
    document = (await session.scalars(query)).one_or_none()
    if document is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    return document


async def upload_scope(
    session: AsyncSession,
    identity: CurrentIdentity,
    base_id: int,
    folder_id: int | None = None,
    *,
    write: bool = True,
) -> None:
    base = await get_knowledge_base(session, identity, base_id)
    if write and not AuthorizationService.can_manage_content(identity, base):
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    if folder_id is not None:
        folder = await session.scalar(
            select(Folder.id).where(
                Folder.id == folder_id,
                Folder.knowledge_base_id == base_id,
                Folder.deleted_at.is_(None),
            )
        )
        if folder is None:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)


async def save_uploads(
    session: AsyncSession,
    identity: CurrentIdentity,
    base_id: int,
    key: str,
    files: list[UploadedFile],
    fields: dict[str, str],
    bucket: str,
    tokens: Tokens,
) -> dict[str, Any]:
    try:
        folder_id = parse_id(fields["folder_id"]) if fields.get("folder_id") else None
        if fields.get("name") and len(files) != 1:
            raise ValueError("batch display name is ambiguous")
    except ValueError:
        raise APIError(ErrorCode.VALIDATION_ERROR) from None
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ORGANIZATION_LOCK})
    identity = await AuthorizationService.revalidate(session, identity)
    await upload_scope(session, identity, base_id, folder_id)
    # Metadata and digests only. Neither file bytes nor object keys enter this record.
    fingerprint = tokens.digest(
        "upload",
        json.dumps(
            {
                "base_id": str(base_id),
                "fields": fields,
                "files": [
                    {
                        "name": f.filename,
                        "mime": f.declared_mime,
                        "size": f.size,
                        "sha256": f.digest,
                    }
                    for f in files
                ],
            },
            sort_keys=True,
        ).encode(),
    )
    prior = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.actor_user_id == identity.user.id,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    if prior is not None:
        if prior.operation_name != "document.upload" or not hmac.compare_digest(
            prior.request_fingerprint, fingerprint
        ):
            raise APIError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
        return prior.response_data
    # A transient failure rolls back the whole batch. Successful retries then commit
    # resources and their replay response together, with no partial unrecorded outcome.
    if any(file.error == ErrorCode.STORAGE_UNAVAILABLE for file in files):
        raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
    results: list[dict[str, Any]] = []
    for index, file in enumerate(files):
        result: dict[str, Any] = {"index": index, "filename": file.filename}
        error = file.error
        name = fields.get("name") or file.filename
        if error is None:
            try:
                name_key = normalize_name(name, max_length=255)
                if len(name) > 255 or any(char in name for char in ("/", "\\")):
                    raise ValueError("invalid name")
            except ValueError:
                error = ErrorCode.VALIDATION_ERROR
        if error is None:
            existing = await session.scalar(
                select(Document).where(
                    Document.knowledge_base_id == base_id,
                    Document.folder_id == folder_id,
                    Document.name_key == name_key,
                    Document.deleted_at.is_(None),
                )
            )
            if existing is not None:
                results.append(
                    {
                        **result,
                        "status": "name_conflict",
                        "document_id": str(existing.id),
                        "name": existing.name,
                        "error_code": ErrorCode.DOCUMENT_NAME_CONFLICT,
                    }
                )
                continue
            assert file.key is not None
            try:
                async with session.begin_nested():
                    original = StorageObject(
                        bucket_name=bucket,
                        object_key=file.key,
                        object_type="original",
                        size_bytes=file.size,
                        content_type=file.mime_type,
                        content_sha256=file.digest,
                        status="available",
                    )
                    session.add(original)
                    await session.flush()
                    result.update(
                        await create_document(
                            session,
                            identity,
                            base_id,
                            folder_id,
                            name,
                            fields.get("description"),
                            original,
                            file.filename,
                            file.extension,
                        )
                    )
            except IntegrityError as exc:
                if getattr(exc.orig, "sqlstate", None) != "23505":
                    raise
                error = ErrorCode.DOCUMENT_NAME_CONFLICT
        if error is not None:
            result.update(status="rejected", error_code=error, message=ERRORS[error][1])
        results.append(result)
    response = {"items": results}
    session.add(
        IdempotencyRecord(
            actor_user_id=identity.user.id,
            idempotency_key=key,
            operation_name="document.upload",
            request_fingerprint=fingerprint,
            resource_id=base_id,
            response_data=response,
        )
    )
    await session.flush()
    return response


async def create_document(
    session: AsyncSession,
    identity: CurrentIdentity,
    base_id: int,
    folder_id: int | None,
    name: str,
    description: str | None,
    original: StorageObject,
    file_name: str,
    file_extension: str,
    trigger_type: str = "initial_upload",
) -> dict[str, Any]:
    """Commit an already verified original and its ingestion work in the caller's transaction."""
    original.status, original.retention_until = "available", None
    document = Document(
        knowledge_base_id=base_id,
        folder_id=folder_id,
        name=name,
        name_key=normalize_name(name, max_length=255),
        description=description,
        created_by=identity.user.id,
    )
    session.add(document)
    await session.flush()
    revision = Revision(
        document_id=document.id,
        revision_no=1,
        trigger_type=trigger_type,
        original_object_id=original.id,
        original_file_name=file_name,
        file_extension=file_extension,
        mime_type=original.content_type,
        size_bytes=original.size_bytes,
        content_sha256=original.content_sha256,
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
            revision_id=revision.id,
            stage="parse",
            unit_key="document",
            latest_task_id=task.id,
        )
    )
    session.add(processing_event(document.id, revision.id, task.id))
    await session.flush()
    return {
        "status": "accepted",
        "document_id": str(document.id),
        "processing_task_id": str(task.id),
    }


def processing_event(document_id: int, revision_id: int, task_id: int) -> OutboxEvent:
    return OutboxEvent(
        aggregate_type="document_revision",
        aggregate_id=revision_id,
        event_type="document.processing.requested",
        payload={
            "document_id": str(document_id),
            "revision_id": str(revision_id),
            "task_id": str(task_id),
        },
        deduplication_key=f"document.processing:{task_id}",
    )


def utc(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


async def document_output(session: AsyncSession, document: Document) -> dict[str, Any]:
    from enterprise_mind.knowledge.models import DeletionBatch

    batch = (
        await session.get(DeletionBatch, document.deletion_batch_id)
        if document.deletion_batch_id
        else None
    )
    revision = (
        await session.scalars(
            select(Revision).where(
                Revision.document_id == document.id,
                Revision.revision_no == document.latest_revision_no,
            )
        )
    ).one()
    return {
        "id": str(document.id),
        "knowledge_base_id": str(document.knowledge_base_id),
        "folder_id": str(document.folder_id) if document.folder_id else None,
        "name": document.name,
        "description": document.description,
        "created_by": str(document.created_by),
        "deleted_at": utc(document.deleted_at),
        "purge_after": utc(document.purge_after),
        "deletion_batch_id": str(document.deletion_batch_id)
        if document.deletion_batch_id
        else None,
        "is_deletion_root": bool(
            batch and batch.root_type == "document" and batch.root_id == document.id
        ),
        "processing_status": "recycle_bin" if document.deleted_at else document.processing_status,
        "file_extension": revision.file_extension,
        "mime_type": revision.mime_type,
        "size_bytes": revision.size_bytes,
        "row_version": document.row_version,
        "created_at": utc(document.created_at),
        "updated_at": utc(document.updated_at),
    }


async def processing_output(session: AsyncSession, document: Document) -> dict[str, Any]:
    task = (
        await session.scalars(
            select(ProcessingTask)
            .join(Revision, Revision.id == ProcessingTask.revision_id)
            .where(
                Revision.document_id == document.id,
                Revision.revision_no == document.latest_revision_no,
                ProcessingTask.task_type != "preview_rebuild",
            )
            .order_by(ProcessingTask.id.desc())
            .limit(1)
        )
    ).one()
    units = list(
        (
            await session.scalars(
                select(ProcessingUnit)
                .where(ProcessingUnit.latest_task_id == task.id)
                .order_by(ProcessingUnit.id)
            )
        ).all()
    )
    failed = [unit for unit in units if unit.status == "failed"]
    return {
        "document_id": str(document.id),
        "processing_task_id": str(task.id),
        "processing_status": document.processing_status,
        "task_status": task.status,
        "current_stage": task.current_stage,
        "stage_current": task.stage_current,
        "stage_total": task.stage_total,
        "failed_unit_count": len(failed),
        "failed_units": [
            {
                "id": str(u.id),
                "stage": u.stage,
                "source_unit_id": str(u.source_unit_id) if u.source_unit_id else None,
                "error_code": u.error_code,
                "error_message": u.error_message,
            }
            for u in failed
        ],
        "units": [
            {
                "id": str(u.id),
                "stage": u.stage,
                "status": u.status,
                "current": u.current,
                "total": u.total,
            }
            for u in units
        ],
        "updated_at": utc(task.updated_at),
    }
