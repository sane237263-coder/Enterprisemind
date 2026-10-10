"""Preview-confirmed import of a private generated file into the knowledge base."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.agent.generation_models import GeneratedFile, GenerationTask
from enterprise_mind.agent.service import scope_version
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.generation import service as generation_service
from enterprise_mind.identity.security import normalize_name, parse_id
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.models import Document, Folder, StorageObject
from enterprise_mind.ingestion.service import create_document, upload_scope, utc
from enterprise_mind.ingestion.storage import ObjectStorage
from enterprise_mind.knowledge import lifecycle as domain
from enterprise_mind.knowledge.models import KnowledgeBase
from enterprise_mind.knowledge.pending_models import PendingAction, PendingActionTarget
from enterprise_mind.presentation import format_beijing_time

OPERATION = "document.generated_write"


def _arguments(action: PendingAction) -> dict[str, Any]:
    try:
        arguments = action.bound_arguments["binding"]["arguments"]["arguments"]
    except (KeyError, TypeError):
        raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED) from None
    if not isinstance(arguments, dict):
        raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
    return arguments


def _preview(
    generated: GeneratedFile,
    source: StorageObject,
    base: Any,
    folder: Any,
    name: str,
    expires_at: datetime,
) -> dict[str, Any]:
    return {
        "generated_file_id": str(generated.id),
        "source_file_name": generated.file_name,
        "source_format": generated.file_format,
        "source_size_bytes": generated.size_bytes,
        "source_sha256": generated.content_sha256,
        "source_refs": generated.source_refs or [],
        "target": {
            "knowledge_base_id": str(base.id),
            "knowledge_base_name": base.name,
            "folder_id": str(folder.id) if folder is not None else None,
            "folder_name": folder.name if folder is not None else None,
            "name": name,
            "content_type": source.content_type,
        },
        "expires_at": utc(expires_at),
    }


def _target_snapshot(resource_type: str, resource: Any) -> dict[str, Any]:
    return {
        "resource_type": resource_type,
        "id": str(resource.id),
        "name": resource.name,
        "path": resource.name,
        "row_version": str(resource.row_version),
    }


async def create_import_confirmation(
    session: AsyncSession,
    identity: CurrentIdentity,
    *,
    generated_file_id: int,
    knowledge_base_id: int,
    folder_id: int | None,
    name: str,
    description: str | None,
    storage: ObjectStorage | None,
    conflict_action: str,
    idempotency_key: str,
    ttl_seconds: int,
) -> dict[str, Any]:
    if not idempotency_key or ttl_seconds <= 0 or conflict_action not in {"reject", "overwrite"}:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    identity = await domain.write_guard(session, identity)
    # A generated-file row is only importable after its task has reached the
    # durable success state and still points at the same published object.
    # Checking the task here keeps a forged or prematurely inserted file row
    # from becoming a preview/confirmation source.
    generated = await session.scalar(
        select(GeneratedFile)
        .join(GenerationTask, GenerationTask.id == GeneratedFile.task_id)
        .where(
            GeneratedFile.id == generated_file_id,
            GeneratedFile.owner_user_id == identity.user.id,
            GenerationTask.owner_user_id == identity.user.id,
            GenerationTask.status == "succeeded",
            GenerationTask.file_id == GeneratedFile.storage_object_id,
            GenerationTask.kind == GeneratedFile.generation_kind,
            GenerationTask.format == GeneratedFile.file_format,
        )
    )
    if generated is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    source = await session.get(StorageObject, generated.storage_object_id)
    if (
        source is None
        or source.status != "available"
        or source.content_sha256 != generated.content_sha256
        or source.size_bytes != generated.size_bytes
    ):
        raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
    await generation_service.revalidate_generated_sources(
        session,
        identity,
        list(generated.source_refs or []),
        current_access_only=True,
    )
    base = await domain.resource(
        session, identity, "knowledge_base", knowledge_base_id, recycled=False, write=False
    )
    assert isinstance(base, KnowledgeBase)
    if generated.source_refs and base.type == "public":
        raise APIError(
            ErrorCode.PERMISSION_DENIED,
            {"reason": "包含受限来源的生成内容不能直接写入公共知识库"},
        )
    folder: Folder | None = None
    if folder_id is not None:
        folder_resource = await domain.resource(
            session, identity, "folder", folder_id, recycled=False, write=False
        )
        assert isinstance(folder_resource, Folder)
        folder = folder_resource
        if folder.knowledge_base_id != knowledge_base_id:
            raise APIError(ErrorCode.CROSS_KNOWLEDGE_BASE_MOVE_FORBIDDEN)
    await upload_scope(session, identity, knowledge_base_id, folder_id, write=True)
    try:
        name_key = normalize_name(name, max_length=255)
    except ValueError:
        raise APIError(ErrorCode.VALIDATION_ERROR) from None
    existing = await session.scalar(
        select(Document)
        .where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.folder_id == folder_id,
            Document.name_key == name_key,
            Document.deleted_at.is_(None),
        )
        .with_for_update()
    )
    effective_conflict_action = "overwrite" if existing is not None else "create"
    arguments = {
        "generated_file_id": str(generated.id),
        "storage_object_id": str(generated.storage_object_id),
        "generated_sha256": generated.content_sha256,
        "generated_size_bytes": generated.size_bytes,
        "knowledge_base_id": str(knowledge_base_id),
        "folder_id": str(folder_id) if folder_id is not None else None,
        "name": name.strip(),
        "description": description,
        "file_name": generated.file_name,
        "file_extension": Path(generated.file_name).suffix.lstrip(".").lower()
        or generated.file_format,
        "content_type": source.content_type,
        "conflict_action": effective_conflict_action,
        "existing_document_id": str(existing.id) if existing is not None else None,
        "existing_row_version": existing.row_version if existing is not None else None,
    }
    request = {
        "operation": OPERATION,
        "resource_id": str(generated.id),
        "row_version": "1",
        "arguments": arguments,
    }
    fingerprint = domain.digest(request)
    previous = await session.scalar(
        select(PendingAction).where(
            PendingAction.actor_user_id == identity.user.id,
            PendingAction.idempotency_key == idempotency_key,
        )
    )
    if previous is not None:
        if previous.bound_arguments.get("request_hash") != fingerprint:
            raise APIError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
        return _view(previous)
    now = await session.scalar(select(domain.func_clock()))
    assert isinstance(now, datetime)
    expires_at = now + timedelta(seconds=ttl_seconds)
    if existing is not None and conflict_action != "overwrite":
        raise APIError(
            ErrorCode.DOCUMENT_NAME_CONFLICT,
            {
                "existing_document_id": str(existing.id),
                "existing_row_version": existing.row_version,
                "choices": ["rename", "overwrite"],
            },
        )

    if existing is not None:
        if storage is None:
            raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
        copied: dict[str, Any] | None = None
        try:
            copied = await asyncio.to_thread(_copy_sync, storage, source)
            staged = StorageObject(
                bucket_name=copied["bucket_name"],
                object_key=copied["object_key"],
                object_type="original",
                size_bytes=copied["size_bytes"],
                content_type=copied["content_type"],
                content_sha256=copied["content_sha256"],
                status="staged",
                retention_until=expires_at,
            )
            session.add(staged)
            await session.flush()
            from enterprise_mind.knowledge import confirmation

            preview = _preview(generated, source, base, folder, name.strip(), expires_at)
            preview.update(
                {
                    "conflict_action": "overwrite",
                    "existing_document_id": str(existing.id),
                    "existing_row_version": existing.row_version,
                }
            )
            return await confirmation.create(
                session,
                identity,
                "document.overwrite",
                existing.id,
                existing.row_version,
                idempotency_key,
                arguments={
                    "staged_upload_id": str(staged.id),
                    "file_name": generated.file_name,
                    "file_extension": arguments["file_extension"],
                    "content_sha256": generated.content_sha256,
                },
                ttl_seconds=ttl_seconds,
                preview=preview,
            )
        except BaseException:
            if copied is not None and storage is not None:
                try:
                    await cleanup_generated_copy(storage, copied)
                except BaseException:
                    pass
            raise

    targets = [_target_snapshot("knowledge_base", base)]
    if folder is not None:
        targets.append(_target_snapshot("folder", folder))
    binding = {
        "binding_version": 1,
        "schema_version": 1,
        "operation_name": OPERATION,
        "actor_user_id": str(identity.user.id),
        "permission_scope_version": scope_version(identity),
        "arguments": request,
        "targets": targets,
        "preview": {
            **_preview(generated, source, base, folder, name.strip(), expires_at),
            "conflict_action": "create",
        },
        "expires_at": utc(expires_at),
    }
    action = PendingAction(
        origin="api",
        operation_name=OPERATION,
        actor_user_id=identity.user.id,
        idempotency_key=idempotency_key,
        action_type="confirmation",
        prompt_text=(
            f"确认将“{generated.file_name}”写入“{base.name}"
            + (f"/{folder.name}" if folder is not None else "")
            + f"，文档名为“{name.strip()}”？文件大小 {generated.size_bytes} 字节。"
            f"确认有效期至 {format_beijing_time(expires_at)}。"
        ),
        bound_arguments={"request_hash": fingerprint, "binding": binding},
        binding_hash=domain.digest(binding),
        status="pending",
        expires_at=expires_at,
        created_at=now,
    )
    session.add(action)
    await session.flush()
    for ordinal, target in enumerate(targets):
        session.add(
            PendingActionTarget(
                pending_action_id=action.id,
                ordinal=ordinal,
                resource_type=target["resource_type"],
                resource_id=int(target["id"]),
                display_name=target["name"],
                expected_row_version=int(target["row_version"]),
                target_snapshot=target,
            )
        )
    await session.flush()
    return _view(action)


def _view(action: PendingAction) -> dict[str, Any]:
    from enterprise_mind.knowledge.confirmation import view

    return view(action)


def _copy_sync(storage: ObjectStorage, source: StorageObject) -> dict[str, Any]:
    object_key = storage.new_key("original")
    try:
        with TemporaryDirectory(prefix="em-generated-import-") as directory:
            destination = Path(directory) / "content"
            storage.download(
                source.object_key,
                destination,
                source.size_bytes,
                source.content_sha256 or "",
            )
            with destination.open("rb") as stream:
                storage.upload(object_key, stream, source.content_type)
            storage.verify(object_key, source.size_bytes, source.content_sha256 or "")
    except BaseException:
        try:
            storage.remove(object_key)
        except BaseException:
            pass
        raise
    return {
        "object_key": object_key,
        "bucket_name": storage.bucket,
        "size_bytes": source.size_bytes,
        "content_type": source.content_type,
        "content_sha256": source.content_sha256,
    }


async def prepare_generated_copy(
    session: AsyncSession,
    identity: CurrentIdentity,
    action: PendingAction,
    storage: ObjectStorage,
) -> dict[str, Any]:
    arguments = _arguments(action)
    generated = await session.scalar(
        select(GeneratedFile)
        .join(GenerationTask, GenerationTask.id == GeneratedFile.task_id)
        .where(
            GeneratedFile.id == parse_id(arguments["generated_file_id"]),
            GeneratedFile.owner_user_id == identity.user.id,
            GenerationTask.owner_user_id == identity.user.id,
            GenerationTask.status == "succeeded",
            GenerationTask.file_id == GeneratedFile.storage_object_id,
            GenerationTask.kind == GeneratedFile.generation_kind,
            GenerationTask.format == GeneratedFile.file_format,
        )
    )
    if generated is None or generated.content_sha256 != arguments.get("generated_sha256"):
        raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
    source = await session.get(StorageObject, generated.storage_object_id)
    if (
        source is None
        or source.status != "available"
        or source.bucket_name != storage.bucket
        or source.content_sha256 != generated.content_sha256
        or source.size_bytes != generated.size_bytes
    ):
        raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
    await generation_service.revalidate_generated_sources(
        session,
        identity,
        list(generated.source_refs or []),
        current_access_only=True,
    )
    copied = await asyncio.to_thread(_copy_sync, storage, source)
    copied.update(
        {
            "generated_file_id": str(generated.id),
            "file_name": arguments["file_name"],
            "file_extension": arguments["file_extension"],
        }
    )
    return copied


async def cleanup_generated_copy(storage: ObjectStorage, copied: dict[str, Any]) -> None:
    object_key = copied.get("object_key")
    if isinstance(object_key, str):
        await asyncio.to_thread(storage.remove, object_key)


async def apply_generated_write(
    session: AsyncSession,
    identity: CurrentIdentity,
    action: PendingAction,
    copied: dict[str, Any] | None,
) -> dict[str, Any]:
    if copied is None:
        raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
    arguments = _arguments(action)
    if (
        str(copied.get("generated_file_id")) != arguments.get("generated_file_id")
        or copied.get("content_sha256") != arguments.get("generated_sha256")
        or copied.get("size_bytes") != arguments.get("generated_size_bytes")
    ):
        raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
    base_id = parse_id(arguments["knowledge_base_id"])
    folder_id = parse_id(arguments["folder_id"]) if arguments.get("folder_id") else None
    await upload_scope(session, identity, base_id, folder_id, write=True)
    base = await domain.resource(
        session, identity, "knowledge_base", base_id, recycled=False, write=False
    )
    assert isinstance(base, KnowledgeBase)
    targets = action.bound_arguments["binding"]["targets"]
    if not targets or targets[0].get("row_version") != str(base.row_version):
        raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
    if folder_id is not None:
        folder = await domain.resource(
            session, identity, "folder", folder_id, recycled=False, write=False
        )
        assert isinstance(folder, Folder)
        if folder.knowledge_base_id != base_id:
            raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
        if len(targets) < 2 or targets[1].get("row_version") != str(folder.row_version):
            raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
    name = arguments["name"]
    name_key = normalize_name(name, max_length=255)
    existing = await session.scalar(
        select(Document.id).where(
            Document.knowledge_base_id == base_id,
            Document.folder_id == folder_id,
            Document.name_key == name_key,
            Document.deleted_at.is_(None),
        )
    )
    if existing is not None:
        raise APIError(ErrorCode.DOCUMENT_NAME_CONFLICT)
    original = StorageObject(
        bucket_name=copied["bucket_name"],
        object_key=copied["object_key"],
        object_type="original",
        size_bytes=copied["size_bytes"],
        content_type=copied["content_type"],
        content_sha256=copied["content_sha256"],
        status="available",
    )
    try:
        async with session.begin_nested():
            session.add(original)
            await session.flush()
            result = await create_document(
                session,
                identity,
                base_id,
                folder_id,
                name,
                arguments.get("description"),
                original,
                arguments["file_name"],
                arguments["file_extension"],
                trigger_type="generated_write",
            )
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise APIError(ErrorCode.DOCUMENT_NAME_CONFLICT) from None
        raise
    return {**result, "generated_file_id": str(arguments["generated_file_id"])}


__all__ = [
    "apply_generated_write",
    "cleanup_generated_copy",
    "create_import_confirmation",
    "prepare_generated_copy",
]
