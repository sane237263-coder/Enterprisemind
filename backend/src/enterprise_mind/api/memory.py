from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import select

from enterprise_mind.api.auth import Envelope, ResponseMeta, SessionDependency
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.api.management import Cursor, Identity, Key, Limit
from enterprise_mind.api.management_schemas import Identifier
from enterprise_mind.api.memory_schemas import MemoryCreate, MemoryDelete, MemoryUpdate
from enterprise_mind.api.pagination import Page, PageMeta, Pagination
from enterprise_mind.identity import management as identity_management
from enterprise_mind.identity.security import parse_id
from enterprise_mind.memory import service
from enterprise_mind.memory.models import Memory

router = APIRouter(prefix="/api/v1", tags=["personal memory"])


def meta(request: Request) -> ResponseMeta:
    return ResponseMeta(request_id=request.state.request_id)


@router.get("/me/memories")
async def list_memories(
    request: Request,
    session: SessionDependency,
    identity: Identity,
    limit: Limit = 50,
    cursor: Cursor = None,
    category: str | None = None,
) -> Page[dict[str, Any]]:
    paging = Pagination(
        request.app.state.tokens,
        {"resource": "memories", "actor": str(identity.user.id), "category": category},
    )
    after = paging.decode(cursor)
    rows = await service.list_for_user(
        session,
        identity.user.id,
        category=category,
        limit=limit,
        after_id=int(after),
    )
    more = len(rows) > limit
    values = rows[:limit]
    return Page(
        data=[service.memory_output(item) for item in values],
        meta=PageMeta(
            request_id=request.state.request_id,
            next_cursor=paging.encode(values[-1].id) if more else None,
        ),
    )


@router.post("/me/memories", status_code=201)
async def create_memory(
    body: MemoryCreate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: Key,
) -> Envelope[dict[str, Any]]:
    async def create() -> dict[str, Any]:
        item, status, epoch = await service.save_for_user(
            session,
            identity.user.id,
            content=body.content,
            category=body.category,
            subject_key=body.subject_key,
            origin="manual",
            max_chars=request.app.state.config.phase9.memory_max_chars,
        )
        return {**service.memory_output(item), "write_status": status, "memory_epoch": epoch}

    result = await identity_management.idempotent_create(
        session,
        identity,
        key,
        "memory.create",
        body,
        request.app.state.tokens,
        create,
    )
    return Envelope(data=result, meta=meta(request))


@router.patch("/me/memories/{memory_id}")
async def update_memory(
    memory_id: Identifier,
    body: MemoryUpdate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> Envelope[dict[str, Any]]:
    current = await session.scalar(
        select(Memory).where(Memory.id == parse_id(memory_id), Memory.user_id == identity.user.id)
    )
    if current is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    subject_key = (
        body.subject_key if "subject_key" in body.model_fields_set else current.subject_key
    )
    item, status, epoch = await service.save_for_user(
        session,
        identity.user.id,
        memory_id=current.id,
        expected_revision=body.expected_revision,
        content=body.content,
        category=body.category or current.category,
        subject_key=subject_key,
        origin="manual",
        source_message_id=current.source_message_id,
        max_chars=request.app.state.config.phase9.memory_max_chars,
    )
    return Envelope(
        data={**service.memory_output(item), "write_status": status, "memory_epoch": epoch},
        meta=meta(request),
    )


@router.delete("/me/memories/{memory_id}")
async def delete_memory(
    memory_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    body: MemoryDelete | None = None,
) -> Envelope[dict[str, Any]]:
    epoch = await service.delete_for_user(
        session,
        identity.user.id,
        parse_id(memory_id),
        expected_revision=body.expected_revision if body else None,
    )
    return Envelope(
        data={"id": memory_id, "deleted": True, "memory_epoch": epoch},
        meta=meta(request),
    )


__all__ = ["router"]
