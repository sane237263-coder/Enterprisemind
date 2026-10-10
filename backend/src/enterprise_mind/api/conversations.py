import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_, select

from enterprise_mind.agent import attachments
from enterprise_mind.agent.models import AgentRun, AnswerSource, Conversation, Message
from enterprise_mind.agent.schemas import (
    ConversationCreate,
    ConversationRename,
    DatabasePageRequest,
    MessageCreate,
)
from enterprise_mind.agent.service import (
    ACTIVE,
    conversation,
    conversation_output,
    create_run,
    owned_run,
    run_output,
)
from enterprise_mind.api.auth import SessionDependency
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.api.management import Cursor, Identity, Key, Limit
from enterprise_mind.api.management_schemas import Identifier
from enterprise_mind.api.pagination import Pagination
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.database.pages import DatabasePageStore
from enterprise_mind.database.projection import authorized_schema, revalidate_schema
from enterprise_mind.database.query_context import DatabaseQueryContextStore
from enterprise_mind.identity.management import idempotent_create
from enterprise_mind.identity.security import parse_id
from enterprise_mind.identity.service import authenticate
from enterprise_mind.ingestion.models import ContentElement, Document, SourceAnchor
from enterprise_mind.knowledge.models import KnowledgeBase
from enterprise_mind.knowledge.pending_models import PendingAction
from enterprise_mind.rag.models import Chunk

router = APIRouter(prefix="/api/v1", tags=["conversations"])


def envelope(request: Request, data: Any, **meta: Any) -> dict[str, Any]:
    return {"data": data, "meta": {"request_id": request.state.request_id, **meta}}


@router.get("/conversations")
async def conversations(
    request: Request,
    session: SessionDependency,
    identity: Identity,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> dict[str, Any]:
    paging = Pagination(
        request.app.state.tokens, {"resource": "conversations", "actor": str(identity.user.id)}
    )
    query = select(Conversation).where(Conversation.owner_user_id == identity.user.id)
    stamp = func.coalesce(Conversation.last_message_at, Conversation.created_at)
    if cursor:
        anchor = await conversation(session, identity, paging.decode(cursor))
        before = anchor.last_message_at or anchor.created_at
        query = query.where(or_(stamp < before, and_(stamp == before, Conversation.id < anchor.id)))
    rows = list(
        (
            await session.scalars(
                query.order_by(stamp.desc(), Conversation.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    return envelope(
        request,
        [conversation_output(row) for row in rows[:limit]],
        next_cursor=paging.encode(rows[limit - 1].id) if len(rows) > limit else None,
    )


@router.post("/conversations", status_code=201)
async def create_conversation(
    body: ConversationCreate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: Key,
) -> dict[str, Any]:
    identity = await AuthorizationService.revalidate(session, identity)

    async def create() -> dict[str, Any]:
        item = Conversation(owner_user_id=identity.user.id, title=body.title)
        session.add(item)
        await session.flush()
        return conversation_output(item)

    result = await idempotent_create(
        session,
        identity,
        key,
        "conversation.create",
        body,
        request.app.state.tokens,
        create,
    )
    return envelope(request, result)


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> dict[str, Any]:
    result = conversation_output(await conversation(session, identity, parse_id(conversation_id)))
    latest = await session.scalar(
        select(AgentRun.id)
        .where(AgentRun.conversation_id == parse_id(conversation_id))
        .order_by(AgentRun.id.desc())
        .limit(1)
    )
    result["latest_run_id"] = str(latest) if latest else None
    return envelope(request, result)


@router.patch("/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: Identifier,
    body: ConversationRename,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> dict[str, Any]:
    identity = await AuthorizationService.revalidate(session, identity)
    item = await conversation(session, identity, parse_id(conversation_id), lock=True)
    if item.row_version != body.row_version:
        raise APIError(ErrorCode.ROW_VERSION_CONFLICT)
    item.title, item.row_version, item.updated_at = (
        body.title,
        item.row_version + 1,
        datetime.now(UTC),
    )
    return envelope(request, conversation_output(item))


async def source_output(
    session: Any,
    identity: Any,
    source: AnswerSource,
    page_store: DatabasePageStore | None = None,
    conversation_id: int | None = None,
    query_store: DatabaseQueryContextStore | None = None,
) -> dict[str, Any]:
    if source.source_type == "database":
        from enterprise_mind.database.projection import authorized_schema

        result: dict[str, Any] = {
            "id": str(source.id),
            "ordinal": source.ordinal,
            "source_type": "database",
            "available": False,
        }
        try:
            binding = await authorized_schema(session, identity)
            allowed = {relation.quoted_qualified_name for relation in binding.catalog.relations}
            locator = source.locator_snapshot if isinstance(source.locator_snapshot, dict) else {}
            stored_source_version = locator.get("_source_version")
            stored_permission_fingerprint = locator.get("_permission_fingerprint")
            if (
                source.database_source_id != binding.database_source_id
                or not set(source.referenced_relations or ()) <= allowed
                or stored_source_version != binding.source_version
                or stored_permission_fingerprint != binding.permission_fingerprint
            ):
                return result
        except APIError:
            return result
        page = (
            page_store.get(
                source.database_result_id,
                owner_user_id=identity.user.id,
                conversation_id=conversation_id,
            )
            if page_store is not None
            else None
        )
        context = (
            query_store.get(
                source.database_query_id,
                owner_user_id=identity.user.id,
                conversation_id=conversation_id,
            )
            if query_store is not None and conversation_id is not None
            else None
        )
        can_continue = bool(
            page is not None
            and context is not None
            and source.has_more
            and page.has_more
            and page.continuation_available
            and context.has_more
            and context.continuation_available
            and page.query_id == source.database_query_id
            and context.current_result_id == source.database_result_id
            and context.current_page_index == page.page_index
            and page.schema_snapshot_id == binding.schema_snapshot_id
            and page.schema_snapshot_id == context.prepared.binding.schema_snapshot_id
            and page.source_version == context.prepared.binding.source_version
            and page.permission_fingerprint == context.prepared.binding.permission_fingerprint
        )
        public_locator = {
            key: value
            for key, value in (
                source.locator_snapshot if isinstance(source.locator_snapshot, dict) else {}
            ).items()
            if not str(key).startswith("_")
        }
        result.update(
            available=True,
            source_title=source.source_title,
            database_source_id=str(source.database_source_id),
            query_id=source.database_query_id,
            result_id=source.database_result_id,
            query_kind=source.database_query_kind,
            returned_rows=source.database_row_count,
            schema_snapshot_id=(
                str(source.schema_snapshot_id) if source.schema_snapshot_id else None
            ),
            referenced_relations=source.referenced_relations,
            queried_at=source.queried_at.isoformat() if source.queried_at else None,
            conditions_summary=source.conditions_summary,
            has_more=source.has_more,
            can_continue=can_continue,
            locator=public_locator,
            result_available=(
                page is not None
                and page.schema_snapshot_id == source.schema_snapshot_id
                and page.schema_snapshot_id == binding.schema_snapshot_id
                and page.source_version == binding.source_version
                and page.permission_fingerprint == binding.permission_fingerprint
            ),
        )
        return result
    present = (
        await session.execute(
            select(
                Chunk.id,
                Document.id.label("document_id"),
                KnowledgeBase.id.label("knowledge_base_id"),
                KnowledgeBase.name.label("knowledge_base_name"),
                ContentElement.structured_payload["text_origin"].astext.label("text_origin"),
            )
            .join(
                Document,
                Document.id == Chunk.document_id,
            )
            .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
            .join(
                SourceAnchor,
                SourceAnchor.id == source.anchor_id,
            )
            .outerjoin(ContentElement, ContentElement.id == SourceAnchor.element_id)
            .where(
                Chunk.id == source.chunk_id,
                Document.deleted_at.is_(None),
                AuthorizationService.knowledge_scope(identity),
                SourceAnchor.revision_id == Chunk.revision_id,
            )
        )
    ).one_or_none()
    knowledge_result: dict[str, Any] = {
        "id": str(source.id),
        "ordinal": source.ordinal,
        "source_type": "knowledge",
        "available": present is not None,
    }
    if present is not None:
        knowledge_result.update(
            knowledge_base_id=str(present.knowledge_base_id),
            knowledge_base_name=present.knowledge_base_name,
            document_id=str(present.document_id),
            document_name=source.source_title,
            source_title=source.source_title,
            quote_text=source.quote_text,
            locator=source.locator_snapshot,
            text_origin=present.text_origin or "native",
        )
    return knowledge_result


@router.get("/conversations/{conversation_id}/messages")
async def messages(
    conversation_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> dict[str, Any]:
    item = await conversation(session, identity, parse_id(conversation_id))
    paging = Pagination(
        request.app.state.tokens,
        {
            "resource": "messages",
            "actor": str(identity.user.id),
            "conversation": conversation_id,
        },
    )
    rows = list(
        (
            await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == item.id,
                    Message.id > paging.decode(cursor),
                )
                .order_by(Message.sequence)
                .limit(limit + 1)
            )
        ).all()
    )
    message_items = []
    for row in rows[:limit]:
        sources = list(
            (
                await session.scalars(
                    select(AnswerSource)
                    .where(
                        AnswerSource.message_id == row.id,
                    )
                    .order_by(AnswerSource.ordinal)
                )
            ).all()
        )
        message_items.append(
            {
                "id": str(row.id),
                "sequence": str(row.sequence),
                "role": row.role,
                "message_type": row.message_type,
                "content_text": row.content_text,
                "content_parts": await attachments.output_parts(session, identity, row),
                "status": row.status,
                "created_at": row.created_at,
                "completed_at": row.completed_at,
                "sources": [
                    await source_output(
                        session,
                        identity,
                        source,
                        request.app.state.database_pages,
                        row.conversation_id,
                        request.app.state.database_queries,
                    )
                    for source in sources
                ],
            }
        )
    return envelope(
        request,
        message_items,
        next_cursor=paging.encode(rows[limit - 1].id) if len(rows) > limit else None,
    )


@router.post("/conversations/{conversation_id}/messages", status_code=202)
async def post_message(
    conversation_id: Identifier,
    body: MessageCreate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: Key,
) -> dict[str, Any]:
    identity = await AuthorizationService.revalidate(session, identity)
    await conversation(session, identity, parse_id(conversation_id))
    if len(body.content_text) > request.app.state.agent.config.max_message_chars:
        raise APIError(ErrorCode.VALIDATION_ERROR)

    async def create() -> dict[str, Any]:
        parts = await attachments.message_parts(
            session, identity, parse_id(conversation_id), body.staged_upload_ids
        )
        return await create_run(
            session, identity, parse_id(conversation_id), body.content_text, parts
        )

    result = await idempotent_create(
        session,
        identity,
        key,
        f"conversation.message:{conversation_id}",
        body,
        request.app.state.tokens,
        create,
    )
    return envelope(request, result)


@router.post(
    "/conversations/{conversation_id}/database-queries/{query_id}/pages",
    status_code=202,
)
async def continue_database_page(
    conversation_id: Identifier,
    query_id: str,
    body: DatabasePageRequest,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: Key,
) -> dict[str, Any]:
    """Create one run from an explicit query/result binding, never from latest()."""

    if body.query_id != query_id:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    identity = await AuthorizationService.revalidate(session, identity)
    conversation_number = parse_id(conversation_id)
    await conversation(session, identity, conversation_number)

    async def create() -> dict[str, Any]:
        context = request.app.state.database_queries.get(
            body.query_id,
            owner_user_id=identity.user.id,
            conversation_id=conversation_number,
        )
        page = request.app.state.database_pages.get(
            body.result_id,
            owner_user_id=identity.user.id,
            conversation_id=conversation_number,
        )
        if (
            context is None
            or page is None
            or page.query_id != body.query_id
            or page.result_id != body.result_id
            or context.current_result_id != body.result_id
            or context.current_page_index != page.page_index
            or not context.has_more
            or not page.has_more
            or not context.continuation_available
            or not page.continuation_available
            or page.source_version != context.prepared.binding.source_version
            or page.permission_fingerprint != context.prepared.binding.permission_fingerprint
        ):
            raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
        try:
            current_binding = await authorized_schema(
                session, identity, snapshot_id=context.prepared.binding.schema_snapshot_id
            )
            await revalidate_schema(
                session,
                identity,
                context.prepared.binding,
                context.guarded_query.referenced_relations,
            )
            if (
                current_binding.permission_fingerprint
                != context.prepared.binding.permission_fingerprint
            ):
                raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
        except APIError as error:
            if error.code in {
                ErrorCode.DATABASE_RELATION_FORBIDDEN,
                ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE,
            }:
                raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED) from None
            raise
        return await create_run(
            session,
            identity,
            conversation_number,
            "继续下一页",
            [
                {
                    "type": "database_page",
                    "query_id": body.query_id,
                    "result_id": body.result_id,
                }
            ],
        )

    result = await idempotent_create(
        session,
        identity,
        key,
        f"conversation.database-page:{conversation_id}",
        body,
        request.app.state.tokens,
        create,
    )
    return envelope(request, result)


@router.get("/agent-runs/{run_id}")
async def get_run(
    run_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> dict[str, Any]:
    result = run_output(await owned_run(session, identity, parse_id(run_id)))
    pending = await session.scalar(
        select(PendingAction.id).where(
            PendingAction.agent_run_id == parse_id(run_id), PendingAction.status == "pending"
        )
    )
    result["pending_action_id"] = str(pending) if pending else None
    return envelope(request, result)


@router.post("/agent-runs/{run_id}/stop")
async def stop_run(
    run_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> dict[str, Any]:
    identity = await AuthorizationService.revalidate(session, identity)
    run = await owned_run(session, identity, parse_id(run_id), lock=True)
    if run.status not in (*ACTIVE, "cancelled"):
        raise APIError(ErrorCode.RUN_NOT_STOPPABLE)
    from sqlalchemy import update

    from enterprise_mind.knowledge.pending_models import PendingAction

    await session.execute(
        update(PendingAction)
        .where(PendingAction.agent_run_id == run.id, PendingAction.status == "pending")
        .values(status="cancelled", resolved_at=datetime.now(UTC))
    )
    run.status = "cancelled"
    run.cancel_requested_at = run.cancel_requested_at or datetime.now(UTC)
    run.finished_at = run.finished_at or datetime.now(UTC)
    return envelope(request, run_output(run))


@router.post("/agent-runs/{run_id}/resume", status_code=202)
async def resume_run(
    run_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> dict[str, Any]:
    await owned_run(session, identity, parse_id(run_id))
    await request.app.state.agent.resume(parse_id(run_id), identity)
    run = await owned_run(session, identity, parse_id(run_id))
    return envelope(request, run_output(run))


@router.get("/agent-runs/{run_id}/events")
async def run_events(
    run_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> StreamingResponse:
    run_number = parse_id(run_id)
    await owned_run(session, identity, run_number)
    token = request.headers["authorization"].split(" ", 1)[1]
    manager = request.app.state.agent

    def encode(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def stream() -> Any:
        previous: tuple[str, str | None, int | None] | None = None
        try:
            async with manager.bus.subscribe(run_number) as read:
                while not await request.is_disconnected():
                    async with request.app.state.sessions() as current_session:
                        current = await authenticate(
                            current_session, token, request.app.state.tokens
                        )
                        run = await owned_run(current_session, current, run_number)
                        snapshot = run_output(run)
                        action_id = await current_session.scalar(
                            select(PendingAction.id).where(
                                PendingAction.agent_run_id == run.id,
                                PendingAction.status == "pending",
                            )
                        )
                        snapshot["pending_action_id"] = str(action_id) if action_id else None
                    current_state = (run.status, run.current_step, action_id)
                    if current_state != previous:
                        yield encode("run.status", snapshot)
                        if run.current_step:
                            yield encode("step.updated", {"step": run.current_step})
                        previous = current_state
                    if run.status in {"completed", "failed", "cancelled"}:
                        yield encode("run." + run.status, snapshot)
                        return
                    item = await read(manager.config.sse_poll_seconds)
                    if item:
                        # Recheck identity after waiting, before releasing buffered text.
                        async with request.app.state.sessions() as current_session:
                            current = await authenticate(
                                current_session, token, request.app.state.tokens
                            )
                            await owned_run(current_session, current, run_number)
                        yield encode(item["event"], item["data"])
                    else:
                        yield ": keepalive\n\n"
                        await asyncio.sleep(0.05)
        except APIError as error:
            yield encode("run.failed", {"failure_code": error.code.value})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/answer-sources/{source_id}/preview")
async def get_source(
    source_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> dict[str, Any]:
    source = await session.scalar(
        select(AnswerSource)
        .join(Message)
        .join(Conversation)
        .where(
            AnswerSource.id == parse_id(source_id),
            Conversation.owner_user_id == identity.user.id,
        )
    )
    if source is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    source_conversation_id = await session.scalar(
        select(Message.conversation_id).where(Message.id == source.message_id)
    )
    page_store: DatabasePageStore = request.app.state.database_pages
    output = await source_output(
        session,
        identity,
        source,
        page_store,
        source_conversation_id,
        request.app.state.database_queries,
    )
    if source.source_type == "database":
        if not output["available"]:
            raise APIError(ErrorCode.SOURCE_UNAVAILABLE)
        page = page_store.get(
            source.database_result_id,
            owner_user_id=identity.user.id,
            conversation_id=source_conversation_id,
        )
        if page is not None and output["result_available"]:
            output.update(
                columns=list(page.columns),
                rows=list(page.rows),
                returned_rows=page.returned_rows,
                result_available=True,
            )
        else:
            output.update(result_available=False, columns=[], rows=[])
        return envelope(request, output)
    if not output["available"]:
        raise APIError(
            ErrorCode.SOURCE_UNAVAILABLE,
            http_status=410 if source.chunk_id is None or source.anchor_id is None else 404,
        )
    from enterprise_mind.api.previews import preview_data

    chunk = await session.get(Chunk, source.chunk_id)
    assert chunk is not None
    output["preview"] = await preview_data(
        session, identity, chunk.document_id, chunk.revision_id, source.anchor_id
    )
    return envelope(request, output)
