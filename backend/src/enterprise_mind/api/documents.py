import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from enterprise_mind.api.auth import Envelope, ResponseMeta, SessionDependency, bearer
from enterprise_mind.api.document_schemas import (
    UPLOAD_BODY,
    DocumentSummary,
    ProcessingSummary,
    UploadResult,
)
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.api.management import Cursor, Identity, Key, Limit
from enterprise_mind.api.management_schemas import Identifier
from enterprise_mind.api.pagination import Page, PageMeta, Pagination
from enterprise_mind.identity.security import parse_id
from enterprise_mind.identity.service import CurrentIdentity, authenticate
from enterprise_mind.ingestion.models import Document, Revision
from enterprise_mind.ingestion.service import (
    document_output,
    processing_output,
    save_uploads,
    upload_scope,
    visible_document,
)
from enterprise_mind.ingestion.uploads import receive_upload

router = APIRouter(prefix="/api/v1", tags=["documents"])


async def detached_identity(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> CurrentIdentity:
    if credentials is None:
        raise APIError(ErrorCode.AUTH_REQUIRED)
    async with request.app.state.sessions() as session, session.begin():
        return await authenticate(session, credentials.credentials, request.app.state.tokens)


@router.post(
    "/knowledge-bases/{knowledge_base_id}/document-uploads",
    status_code=202,
    openapi_extra=UPLOAD_BODY,
)
async def upload(
    knowledge_base_id: Identifier,
    request: Request,
    key: Key,
    identity: Annotated[CurrentIdentity, Depends(detached_identity)],
) -> Envelope[UploadResult]:
    storage = request.app.state.storage
    if storage is None:
        raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
    async with request.app.state.sessions() as session, session.begin():
        await upload_scope(session, identity, parse_id(knowledge_base_id))
    # No database transaction is held while receiving or transferring file content.
    files, fields = await receive_upload(request, storage, request.app.state.config.upload)
    async with request.app.state.sessions() as session, session.begin():
        result = await save_uploads(
            session,
            identity,
            parse_id(knowledge_base_id),
            key,
            files,
            fields,
            storage.bucket,
            request.app.state.tokens,
        )
    return Envelope(
        data=UploadResult.model_validate(result),
        meta=ResponseMeta(request_id=request.state.request_id),
    )


@router.get("/knowledge-bases/{knowledge_base_id}/documents")
async def documents(
    knowledge_base_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    folder_id: Identifier | None = None,
    scope: Literal["active", "recycle_bin"] = "active",
    processing_status: str | None = None,
    file_extension: str | None = None,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> Page[DocumentSummary]:
    base_id = parse_id(knowledge_base_id)
    folder = parse_id(folder_id) if folder_id else None
    from enterprise_mind.knowledge.lifecycle import resource

    if scope == "active":
        await upload_scope(session, identity, base_id, folder, write=False)
    else:
        await resource(session, identity, "knowledge_base", base_id, write=False)
    if processing_status is not None and processing_status not in {
        "pending",
        "processing",
        "success",
        "partial_success",
        "failed",
    }:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    pagination = Pagination(
        request.app.state.tokens,
        {
            "resource": "documents",
            "actor": str(identity.user.id),
            "base": knowledge_base_id,
            "folder": folder_id,
            "status": processing_status,
            "scope": scope,
            "extension": file_extension,
        },
    )
    query = (
        select(Document)
        .join(
            Revision,
            (Revision.document_id == Document.id)
            & (Revision.revision_no == Document.latest_revision_no),
        )
        .where(
            Document.knowledge_base_id == base_id,
            Document.deleted_at.is_(None)
            if scope == "active"
            else Document.deleted_at.is_not(None),
            Document.id > pagination.decode(cursor),
        )
    )
    if folder_id is not None:
        query = query.where(Document.folder_id == folder)
    if processing_status is not None:
        query = query.where(Document.processing_status == processing_status)
    if file_extension is not None:
        query = query.where(Revision.file_extension == file_extension)
    rows = list((await session.scalars(query.order_by(Document.id).limit(limit + 1))).all())
    return Page(
        data=[
            DocumentSummary.model_validate(await document_output(session, row))
            for row in rows[:limit]
        ],
        meta=PageMeta(
            request_id=request.state.request_id,
            next_cursor=pagination.encode(rows[limit - 1].id) if len(rows) > limit else None,
        ),
    )


@router.get("/documents/{document_id}")
async def document(
    document_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    scope: Literal["active", "recycle_bin"] = "active",
) -> Envelope[DocumentSummary]:
    from enterprise_mind.knowledge.lifecycle import resource

    target = await resource(
        session,
        identity,
        "document",
        parse_id(document_id),
        recycled=scope == "recycle_bin",
        write=False,
    )
    assert isinstance(target, Document)
    return Envelope(
        data=DocumentSummary.model_validate(await document_output(session, target)),
        meta=ResponseMeta(request_id=request.state.request_id),
    )


@router.get("/documents/{document_id}/processing")
async def processing(
    document_id: Identifier, request: Request, session: SessionDependency, identity: Identity
) -> Envelope[ProcessingSummary]:
    target = await visible_document(session, identity, parse_id(document_id))
    return Envelope(
        data=ProcessingSummary.model_validate(await processing_output(session, target)),
        meta=ResponseMeta(request_id=request.state.request_id),
    )


class ProcessingSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    type: Literal["subscribe"]
    access_token: str = Field(min_length=1, max_length=4096, repr=False)
    document_ids: list[Identifier] = Field(min_length=1, max_length=100)


@router.websocket("/ws/document-processing")
async def processing_updates(socket: WebSocket) -> None:
    origin = socket.app.state.public_origin
    if socket.query_params or (origin and socket.headers.get("origin") != origin.rstrip("/")):
        await socket.close(code=1008)
        return
    await socket.accept()
    try:
        async with asyncio.timeout(10):
            raw = await socket.receive_text()
        if len(raw) > 16384:
            raise ValueError("subscription too large")
        subscription = ProcessingSubscription.model_validate_json(raw)
        previous: dict[str, dict[str, Any]] = {}
        while True:
            snapshots = {}
            # Recheck account, credential version and resource scope on every notification pass.
            async with socket.app.state.sessions() as session, session.begin():
                identity = await authenticate(
                    session, subscription.access_token, socket.app.state.tokens
                )
                for identifier in dict.fromkeys(subscription.document_ids):
                    document = await visible_document(session, identity, parse_id(identifier))
                    state = await processing_output(session, document)
                    snapshots[identifier] = {
                        key: state[key]
                        for key in (
                            "document_id",
                            "processing_task_id",
                            "processing_status",
                            "current_stage",
                            "stage_current",
                            "stage_total",
                            "failed_unit_count",
                            "updated_at",
                        )
                    }
            for identifier, state in snapshots.items():
                if previous.get(identifier) != state:
                    await socket.send_json({"type": "document.processing", "data": state})
            previous = snapshots
            try:
                async with asyncio.timeout(1):
                    message = await socket.receive_text()
                if message != "ping":
                    raise ValueError("unexpected message")
            except TimeoutError:
                pass
    except WebSocketDisconnect:
        return
    except (APIError, ValueError, ValidationError, TimeoutError):
        await socket.close(code=1008)
    except Exception:
        await socket.close(code=1011)
