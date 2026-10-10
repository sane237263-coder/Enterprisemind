from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from enterprise_mind.agent import attachments
from enterprise_mind.agent.service import conversation
from enterprise_mind.api.auth import SessionDependency
from enterprise_mind.api.documents import detached_identity
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.api.management import Cursor, Identity, Key, Limit
from enterprise_mind.api.management_schemas import Identifier
from enterprise_mind.api.pagination import Pagination
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.models import StorageObject
from enterprise_mind.ingestion.uploads import receive_upload
from enterprise_mind.knowledge.lifecycle import func_clock

router = APIRouter(prefix="/api/v1", tags=["conversation attachments"])

UPLOAD_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["files"],
                    "additionalProperties": False,
                    "properties": {
                        "files": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 10,
                            "items": {"type": "string", "format": "binary"},
                        }
                    },
                }
            }
        },
    }
}


@router.post(
    "/conversations/{conversation_id}/attachments", status_code=201, openapi_extra=UPLOAD_BODY
)
async def upload_attachments(
    conversation_id: Identifier,
    request: Request,
    key: Key,
    identity: Annotated[CurrentIdentity, Depends(detached_identity)],
) -> dict[str, Any]:
    storage = request.app.state.storage
    if storage is None:
        raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
    async with request.app.state.sessions() as session:
        await conversation(session, identity, int(conversation_id))
    config = request.app.state.config.upload.model_copy(
        update={
            "max_files": min(
                request.app.state.config.upload.max_files, attachments.MAX_ATTACHMENTS
            ),
        }
    )
    # Authenticate before streaming and revalidate after it; no DB transaction spans MinIO I/O.
    files, _ = await receive_upload(request, storage, config, allowed_fields=set())
    async with request.app.state.sessions() as session, session.begin():
        result = await attachments.stage(
            session,
            identity,
            int(conversation_id),
            key,
            files,
            storage.bucket,
            request.app.state.tokens,
            request.app.state.agent.config.attachment_ttl_seconds,
        )
    return {"data": result, "meta": {"request_id": request.state.request_id}}


@router.get("/conversations/{conversation_id}/attachments")
async def list_attachments(
    conversation_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> dict[str, Any]:
    await conversation(session, identity, int(conversation_id))
    paging = Pagination(
        request.app.state.tokens,
        {
            "resource": "attachments",
            "actor": str(identity.user.id),
            "conversation": conversation_id,
        },
    )
    now = await session.scalar(select(func_clock()))
    assert now is not None
    rows = list(
        (
            await session.scalars(
                select(StorageObject)
                .where(
                    StorageObject.attachment_conversation_id == int(conversation_id),
                    StorageObject.status == "staged",
                    StorageObject.retention_until > now,
                    StorageObject.id > paging.decode(cursor),
                )
                .order_by(StorageObject.id)
                .limit(limit + 1)
            )
        ).all()
    )
    return {
        "data": [attachments.view(obj, now) for obj in rows[:limit]],
        "meta": {
            "request_id": request.state.request_id,
            "next_cursor": paging.encode(rows[limit - 1].id) if len(rows) > limit else None,
        },
    }
