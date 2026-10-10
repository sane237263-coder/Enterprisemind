"""Authenticated draft, render-task, and generated-file endpoints."""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from starlette.background import BackgroundTask

from enterprise_mind.agent.generation_models import GeneratedFile
from enterprise_mind.agent.service import conversation
from enterprise_mind.api.auth import Envelope, ResponseMeta, SessionDependency, bearer
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.api.generation_schemas import (
    DatabaseExportTaskCreate,
    DraftCreate,
    DraftUpdate,
    GeneratedFileImportPreviewCreate,
    RenderTaskCreate,
)
from enterprise_mind.api.management import Identity, Key
from enterprise_mind.api.management_schemas import Identifier
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.generation import import_service, service
from enterprise_mind.identity import management as identity_management
from enterprise_mind.identity.security import parse_id
from enterprise_mind.identity.service import CurrentIdentity, authenticate
from enterprise_mind.ingestion.models import OutboxEvent, StorageObject

router = APIRouter(prefix="/api/v1", tags=["generation"])


def meta(request: Request) -> ResponseMeta:
    return ResponseMeta(request_id=request.state.request_id)


async def _generation_detached_identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> CurrentIdentity:
    if credentials is None:
        raise APIError(ErrorCode.AUTH_REQUIRED)
    async with request.app.state.sessions() as session, session.begin():
        return await authenticate(session, credentials.credentials, request.app.state.tokens)


DetachedIdentity = Annotated[CurrentIdentity, Depends(_generation_detached_identity)]


@router.post("/conversations/{conversation_id}/drafts", status_code=201)
async def create_draft(
    conversation_id: Identifier,
    body: DraftCreate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: Key,
) -> Envelope[dict[str, Any]]:
    # Serialize retries before consulting their cached result, and check the
    # target even on replay. The existing operation/fingerprint stays compatible
    # with persisted draft requests from earlier releases.
    identity = await AuthorizationService.revalidate(session, identity)
    await conversation(session, identity, parse_id(conversation_id), lock=True)

    async def create() -> dict[str, Any]:
        item = await service.create_draft(
            session,
            identity,
            title=body.title,
            body_markdown=body.body_markdown,
            tables=body.tables,
            conversation_id=parse_id(conversation_id),
        )
        return service.draft_output(item)

    result = await identity_management.idempotent_create(
        session,
        identity,
        key,
        "draft.create",
        body,
        request.app.state.tokens,
        create,
    )
    if result.get("conversation_id") != conversation_id:
        raise APIError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
    return Envelope(data=result, meta=meta(request))


@router.get("/drafts/{draft_id}")
async def get_draft(
    draft_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> Envelope[dict[str, Any]]:
    item = await service.get_draft(session, identity, parse_id(draft_id), current_access_only=True)
    return Envelope(data=service.draft_output(item), meta=meta(request))


@router.patch("/drafts/{draft_id}")
async def update_draft(
    draft_id: Identifier,
    body: DraftUpdate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> Envelope[dict[str, Any]]:
    values = body.model_dump(exclude_unset=True)
    item = await service.update_draft(
        session,
        identity,
        parse_id(draft_id),
        expected_revision=body.expected_revision,
        title=values.get("title"),
        body_markdown=values.get("body_markdown"),
        tables=values.get("tables"),
    )
    return Envelope(data=service.draft_output(item), meta=meta(request))


@router.post("/drafts/{draft_id}/render", status_code=202)
async def render_draft(
    draft_id: Identifier,
    body: RenderTaskCreate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: Key,
) -> Envelope[dict[str, Any]]:
    task = await service.create_render_task(
        session,
        identity,
        draft_id=parse_id(draft_id),
        expected_revision=body.expected_revision,
        file_format=body.format,
        file_name=body.file_name,
        idempotency_key=key,
    )
    return Envelope(data=service.task_output(task), meta=meta(request))


async def _schedule_generation_event(app: Any, event_id: int, task_id: int) -> None:
    worker = getattr(app.state, "generation_worker", None)
    if worker is None:
        return
    jobs: dict[int, asyncio.Task[bool]] = app.state.phase9_generation_jobs

    async def run() -> bool:
        try:
            result = cast(bool, await worker.handle_event(event_id))
        except asyncio.CancelledError:
            await _finish_generation_event(app, event_id, failed=True)
            raise
        except Exception:
            await _finish_generation_event(app, event_id, failed=True)
            raise
        await _finish_generation_event(app, event_id, failed=False)
        return result

    job = asyncio.create_task(run(), name=f"generation:{task_id}")
    jobs[task_id] = job

    def finished(completed: asyncio.Task[bool]) -> None:
        jobs.pop(task_id, None)
        if not completed.cancelled():
            with suppress(BaseException):
                completed.exception()

    job.add_done_callback(finished)


async def _finish_generation_event(app: Any, event_id: int, *, failed: bool) -> None:
    async with app.state.sessions() as session, session.begin():
        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
        )
        if event is None:
            return
        event.lease_owner = event.lease_expires_at = None
        if failed:
            event.status = "failed"
            event.last_error = "生成任务在 API 进程中未完成。"
            event.next_attempt_at = datetime.now(UTC)
        else:
            event.status = "published"
            event.published_at = datetime.now(UTC)
            event.last_error = None


@router.post("/conversations/{conversation_id}/database-exports", status_code=202)
async def create_database_export(
    conversation_id: Identifier,
    body: DatabaseExportTaskCreate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: Key,
) -> Envelope[dict[str, Any]]:
    if request.app.state.database_client is None:
        raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE)
    if request.app.state.storage is None:
        raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
    task = await service.create_database_export_task(
        session,
        identity,
        conversation_id=parse_id(conversation_id),
        query_id=body.query_id,
        result_id=body.result_id,
        scope=body.scope,
        file_format=body.format,
        file_name=body.file_name,
        idempotency_key=key,
        row_limit=body.row_limit,
        page_store=request.app.state.database_pages,
        query_store=request.app.state.database_queries,
        runtime_store=request.app.state.export_plans,
        plan_owner=request.app.state.database_client,
    )
    # Database exports are consumed by the dedicated queue Worker.  Keeping
    # this event pending until Dispatcher publication is what makes the
    # API-process -> queue -> independent Worker plan-owner path explicit.
    return Envelope(data=service.task_output(task), meta=meta(request))


@router.get("/generation-tasks/{task_id}")
async def get_generation_task(
    task_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> Envelope[dict[str, Any]]:
    current = await AuthorizationService.revalidate(session, identity)
    task = await service.get_task(session, current, parse_id(task_id))
    files = list(
        (
            await session.scalars(
                select(GeneratedFile)
                .where(GeneratedFile.task_id == task.id)
                .order_by(GeneratedFile.file_format, GeneratedFile.id)
            )
        ).all()
    )
    for file in files:
        await service.revalidate_generated_sources(
            session, current, list(file.source_refs or []), current_access_only=True
        )
    return Envelope(data=service.task_output(task, files=files), meta=meta(request))


@router.post("/generated-files/{file_id}/import-previews", status_code=202)
async def create_generated_file_import_preview(
    file_id: Identifier,
    body: GeneratedFileImportPreviewCreate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: Key,
) -> Envelope[dict[str, Any]]:
    result = await import_service.create_import_confirmation(
        session,
        identity,
        generated_file_id=parse_id(file_id),
        knowledge_base_id=parse_id(body.knowledge_base_id),
        folder_id=parse_id(body.folder_id) if body.folder_id is not None else None,
        name=body.name,
        description=body.description,
        storage=request.app.state.storage,
        conflict_action=body.conflict_action,
        idempotency_key=key,
        ttl_seconds=request.app.state.agent.config.pending_action_ttl_seconds,
    )
    return Envelope(data=result, meta=meta(request))


@router.post("/generation-tasks/{task_id}/cancel")
async def cancel_generation_task(
    task_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> Envelope[dict[str, Any]]:
    task = await service.cancel_task(session, identity, parse_id(task_id))
    files = list(
        (
            await session.scalars(
                select(GeneratedFile)
                .where(GeneratedFile.task_id == task.id)
                .order_by(GeneratedFile.file_format, GeneratedFile.id)
            )
        ).all()
    )
    for file in files:
        await service.revalidate_generated_sources(
            session, identity, list(file.source_refs or []), current_access_only=True
        )
    return Envelope(data=service.task_output(task, files=files), meta=meta(request))


@router.get("/generated-files/{file_id}/download")
async def download_generated_file(
    file_id: Identifier,
    request: Request,
    identity: DetachedIdentity,
) -> FileResponse:
    storage = request.app.state.storage
    if storage is None:
        raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
    async with request.app.state.sessions() as session, session.begin():
        current = await AuthorizationService.revalidate(session, identity)
        file = await session.scalar(
            select(GeneratedFile).where(
                GeneratedFile.id == parse_id(file_id),
                GeneratedFile.owner_user_id == current.user.id,
            )
        )
        if file is None:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        obj = await session.get(StorageObject, file.storage_object_id)
        if (
            obj is None
            or obj.status != "available"
            or obj.bucket_name != storage.bucket
            or not obj.content_sha256
        ):
            raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
        await service.revalidate_generated_sources(
            session, current, list(file.source_refs or []), current_access_only=True
        )
        object_key, object_size, object_digest = obj.object_key, obj.size_bytes, obj.content_sha256
        file_name = file.file_name
    directory = TemporaryDirectory(prefix="em-generated-")
    destination = Path(directory.name) / "content"
    try:
        await asyncio.to_thread(
            storage.download, object_key, destination, object_size, object_digest
        )
        async with request.app.state.sessions() as session, session.begin():
            current = await AuthorizationService.revalidate(session, identity)
            visible = await session.scalar(
                select(GeneratedFile.id).where(
                    GeneratedFile.id == parse_id(file_id),
                    GeneratedFile.owner_user_id == current.user.id,
                )
            )
            if visible is None:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            await service.revalidate_generated_sources(
                session, current, list(file.source_refs or []), current_access_only=True
            )
        return FileResponse(
            destination,
            media_type=obj.content_type,
            filename=file_name,
            content_disposition_type="attachment",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
            background=BackgroundTask(directory.cleanup),
        )
    except BaseException:
        directory.cleanup()
        raise


@router.get("/generated-files/{file_id}/preview")
async def preview_generated_file(
    file_id: Identifier,
    request: Request,
    identity: DetachedIdentity,
) -> FileResponse:
    """Serve a private generated PNG inline after the same source recheck."""

    storage = request.app.state.storage
    if storage is None:
        raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
    async with request.app.state.sessions() as session, session.begin():
        current = await AuthorizationService.revalidate(session, identity)
        file = await session.scalar(
            select(GeneratedFile).where(
                GeneratedFile.id == parse_id(file_id),
                GeneratedFile.owner_user_id == current.user.id,
                GeneratedFile.file_format == "png",
                GeneratedFile.generation_kind == "chart_render",
            )
        )
        if file is None:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        obj = await session.get(StorageObject, file.storage_object_id)
        if (
            obj is None
            or obj.status != "available"
            or obj.bucket_name != storage.bucket
            or obj.content_type != "image/png"
            or not obj.content_sha256
        ):
            raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
        await service.revalidate_generated_sources(
            session, current, list(file.source_refs or []), current_access_only=True
        )
        object_key, object_size, object_digest = obj.object_key, obj.size_bytes, obj.content_sha256
    directory = TemporaryDirectory(prefix="em-chart-preview-")
    destination = Path(directory.name) / "chart.png"
    try:
        await asyncio.to_thread(
            storage.download, object_key, destination, object_size, object_digest
        )
        async with request.app.state.sessions() as session, session.begin():
            current = await AuthorizationService.revalidate(session, identity)
            visible_file = await session.scalar(
                select(GeneratedFile).where(
                    GeneratedFile.id == parse_id(file_id),
                    GeneratedFile.owner_user_id == current.user.id,
                    GeneratedFile.file_format == "png",
                    GeneratedFile.generation_kind == "chart_render",
                )
            )
            if visible_file is None:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            await service.revalidate_generated_sources(
                session,
                current,
                list(visible_file.source_refs or []),
                current_access_only=True,
            )
        return FileResponse(
            destination,
            media_type="image/png",
            content_disposition_type="inline",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "default-src 'none'; img-src 'self'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
            background=BackgroundTask(directory.cleanup),
        )
    except BaseException:
        directory.cleanup()
        raise


__all__ = ["router"]
