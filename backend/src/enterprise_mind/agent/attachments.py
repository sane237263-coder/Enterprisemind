"""Owned, expiring attachments. File bytes stay in private object storage."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.agent.generation_models import GenerationTask
from enterprise_mind.agent.models import AgentRun, Message
from enterprise_mind.agent.service import conversation
from enterprise_mind.api.errors import ERRORS, APIError, ErrorCode
from enterprise_mind.api.management_schemas import InputModel
from enterprise_mind.identity.management import idempotent_create
from enterprise_mind.identity.security import Tokens, normalize_name
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.models import Document, StorageObject
from enterprise_mind.ingestion.service import create_document, upload_scope, utc
from enterprise_mind.ingestion.uploads import UploadedFile
from enterprise_mind.knowledge import lifecycle as domain

MAX_ATTACHMENTS = 10


class UploadFingerprint(InputModel):
    # Server-built validated metadata; object keys and file bytes are never included.
    files: list[dict[str, Any]]


def view(obj: StorageObject, now: datetime) -> dict[str, Any]:
    available = (
        obj.status == "staged" and obj.retention_until is not None and obj.retention_until > now
    )
    return {
        "type": "attachment",
        "staged_upload_id": str(obj.id),
        "file_name": obj.attachment_file_name,
        "file_extension": obj.attachment_file_extension,
        "size_bytes": obj.size_bytes,
        "available": available,
        "expires_at": utc(obj.retention_until),
    }


async def stage(
    session: AsyncSession,
    identity: CurrentIdentity,
    conversation_id: int,
    key: str,
    files: list[UploadedFile],
    bucket: str,
    tokens: Tokens,
    ttl_seconds: int,
) -> dict[str, Any]:
    identity = await domain.write_guard(session, identity)
    await conversation(session, identity, conversation_id, lock=True)
    if len(files) > MAX_ATTACHMENTS:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    if any(file.error == ErrorCode.STORAGE_UNAVAILABLE for file in files):
        raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
    body = UploadFingerprint(
        files=[
            {"name": f.filename, "mime": f.declared_mime, "size": f.size, "sha256": f.digest}
            for f in files
        ]
    )

    async def create() -> dict[str, Any]:
        now = await session.scalar(select(domain.func_clock()))
        assert isinstance(now, datetime)
        items = []
        for index, file in enumerate(files):
            result: dict[str, Any] = {"index": index, "filename": file.filename}
            if file.error:
                items.append(
                    {
                        **result,
                        "status": "rejected",
                        "error_code": file.error,
                        "message": ERRORS[file.error][1],
                    }
                )
                continue
            assert file.key is not None
            obj = StorageObject(
                bucket_name=bucket,
                object_key=file.key,
                object_type="original",
                size_bytes=file.size,
                content_type=file.mime_type,
                content_sha256=file.digest,
                status="staged",
                retention_until=now + timedelta(seconds=ttl_seconds),
                attachment_conversation_id=conversation_id,
                attachment_file_name=file.filename,
                attachment_file_extension=file.extension,
            )
            session.add(obj)
            await session.flush()
            items.append({**result, **view(obj, now), "status": "accepted"})
        return {"id": str(conversation_id), "items": items}

    return await idempotent_create(
        session, identity, key, f"conversation.attachments:{conversation_id}", body, tokens, create
    )


async def owned(
    session: AsyncSession,
    identity: CurrentIdentity,
    conversation_id: int,
    identifier: int,
    *,
    usable: bool = True,
) -> StorageObject:
    await conversation(session, identity, conversation_id)
    obj = await session.scalar(
        select(StorageObject)
        .where(
            StorageObject.id == identifier,
            StorageObject.attachment_conversation_id == conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if obj is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    now = await session.scalar(select(domain.func_clock()))
    assert isinstance(now, datetime)
    if usable and not view(obj, now)["available"]:
        raise APIError(ErrorCode.ATTACHMENT_UNAVAILABLE)
    return obj


async def message_parts(
    session: AsyncSession,
    identity: CurrentIdentity,
    conversation_id: int,
    identifiers: list[str],
) -> list[dict[str, Any]] | None:
    parts = []
    for identifier in identifiers:
        await owned(session, identity, conversation_id, int(identifier))
        parts.append({"type": "attachment", "staged_upload_id": identifier})
    return parts or None


async def for_run(
    session: AsyncSession,
    identity: CurrentIdentity,
    run_id: int,
    identifier: int,
) -> StorageObject:
    run = await session.get(AgentRun, run_id)
    if run is None or run.actor_user_id != identity.user.id:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    obj = await owned(session, identity, run.conversation_id, identifier)
    current = await session.get(Message, run.user_message_id)
    assert current is not None
    bound = await session.scalar(
        select(Message.id)
        .where(
            Message.conversation_id == run.conversation_id,
            Message.role == "user",
            Message.sequence <= current.sequence,
            Message.content_parts.contains(
                [{"type": "attachment", "staged_upload_id": str(identifier)}]
            ),
        )
        .limit(1)
    )
    if bound is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    return obj


async def catalog(
    session: AsyncSession,
    identity: CurrentIdentity,
    run: AgentRun,
) -> list[dict[str, Any]]:
    await conversation(session, identity, run.conversation_id)
    current = await session.get(Message, run.user_message_id)
    assert current is not None
    now = await session.scalar(select(domain.func_clock()))
    assert isinstance(now, datetime)
    # Only explicit user-message attachments, including earlier clarification turns.
    linked = (
        select(Message.id)
        .where(
            Message.conversation_id == run.conversation_id,
            Message.role == "user",
            Message.sequence <= current.sequence,
            Message.content_parts.contains(
                func.jsonb_build_array(
                    func.jsonb_build_object(
                        "type",
                        "attachment",
                        "staged_upload_id",
                        cast(StorageObject.id, String),
                    )
                )
            ),
        )
        .correlate(StorageObject)
        .exists()
    )
    rows = await session.scalars(
        select(StorageObject)
        .where(
            StorageObject.attachment_conversation_id == run.conversation_id,
            StorageObject.status == "staged",
            StorageObject.retention_until > now,
            linked,
        )
        .order_by(StorageObject.id.desc())
        .limit(50)
    )
    return [view(obj, now) for obj in rows]


async def output_parts(
    session: AsyncSession,
    identity: CurrentIdentity,
    message: Message,
) -> list[dict[str, Any]] | None:
    parts = []
    for part in message.content_parts or []:
        if part.get("type") == "client_action":
            from enterprise_mind.agent import client_actions

            action = await client_actions.output(session, identity, message, part)
            if action:
                parts.append(action)
            continue
        if part.get("type") != "attachment":
            parts.append(part)
            continue
        try:
            obj = await owned(
                session,
                identity,
                message.conversation_id,
                int(part["staged_upload_id"]),
                usable=False,
            )
        except APIError as error:
            if error.code != ErrorCode.RESOURCE_NOT_FOUND:
                raise
            parts.append({**part, "available": False})
        else:
            now = await session.scalar(select(domain.func_clock()))
            assert isinstance(now, datetime)
            parts.append(view(obj, now))
    # Manual exports are durable tasks, not transient browser state. Rebuild
    # their references on history reads, including tasks created before this fix.
    if message.role == "assistant" and any(part.get("type") == "content_draft" for part in parts):
        known = {
            str(part.get("task_id")) for part in parts if part.get("type") == "generated_file_task"
        }
        tasks = await session.scalars(
            select(GenerationTask)
            .where(
                GenerationTask.owner_user_id == identity.user.id,
                GenerationTask.conversation_id == message.conversation_id,
                GenerationTask.source_message_id == message.id,
                GenerationTask.kind == "content_render",
            )
            .order_by(GenerationTask.id)
        )
        for task in tasks:
            if str(task.id) not in known:
                parts.append(
                    {
                        "type": "generated_file_task",
                        "task_id": str(task.id),
                        "kind": task.kind,
                        "format": task.format,
                        "status": task.status,
                        "scope": task.scope,
                    }
                )
    return parts or None


def metadata(obj: StorageObject) -> dict[str, Any]:
    return {
        "staged_upload_id": str(obj.id),
        "file_name": obj.attachment_file_name,
        "file_extension": obj.attachment_file_extension,
        "content_sha256": obj.content_sha256,
    }


async def consume_upload(
    session: AsyncSession,
    identity: CurrentIdentity,
    run_id: int,
    values: dict[str, Any],
) -> dict[str, Any]:
    base_id = int(values["knowledge_base_id"])
    folder_id = int(values["folder_id"]) if values.get("folder_id") else None
    await upload_scope(session, identity, base_id, folder_id)
    objects = []
    for item in values["files"]:
        obj = await for_run(session, identity, run_id, int(item["staged_upload_id"]))
        if any(item[key] != value for key, value in metadata(obj).items()):
            raise APIError(ErrorCode.ATTACHMENT_UNAVAILABLE)
        objects.append(obj)
    results = []
    for item, obj in zip(values["files"], objects, strict=True):
        name = item.get("name") or obj.attachment_file_name
        assert name is not None
        name_key = normalize_name(name, max_length=255)
        existing = await session.scalar(
            select(Document).where(
                Document.knowledge_base_id == base_id,
                Document.folder_id == folder_id,
                Document.name_key == name_key,
                Document.deleted_at.is_(None),
            )
        )
        result: dict[str, Any] = {"staged_upload_id": str(obj.id), "name": name}
        if existing:
            result.update(
                status="name_conflict",
                document_id=str(existing.id),
                error_code=ErrorCode.DOCUMENT_NAME_CONFLICT.value,
            )
        else:
            assert obj.attachment_file_name and obj.attachment_file_extension
            result.update(
                await create_document(
                    session,
                    identity,
                    base_id,
                    folder_id,
                    name,
                    item.get("description"),
                    obj,
                    obj.attachment_file_name,
                    obj.attachment_file_extension,
                )
            )
        results.append(result)
    return {"items": results}
