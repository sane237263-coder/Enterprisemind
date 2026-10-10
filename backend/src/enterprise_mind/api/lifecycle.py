from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, text

from enterprise_mind.agent.models import AgentRun
from enterprise_mind.agent.service import scope_version
from enterprise_mind.api.auth import Envelope, ResponseMeta, SessionDependency
from enterprise_mind.api.documents import detached_identity
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.api.lifecycle_schemas import (
    DocumentUpdate,
    FolderCreate,
    FolderUpdate,
    PendingResponse,
    ResourceVersion,
    RestoreInput,
)
from enterprise_mind.api.management import Cursor, Identity, Key, Limit
from enterprise_mind.api.management_schemas import Identifier
from enterprise_mind.api.pagination import Page, PageMeta, Pagination
from enterprise_mind.generation import import_service
from enterprise_mind.identity.management import idempotent_create
from enterprise_mind.identity.security import parse_id
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.models import Document, Folder, StorageObject
from enterprise_mind.ingestion.uploads import receive_upload
from enterprise_mind.knowledge import confirmation
from enterprise_mind.knowledge import lifecycle as domain
from enterprise_mind.knowledge.pending_models import PendingAction, PendingActionTarget

router = APIRouter(prefix="/api/v1", tags=["lifecycle"])


def envelope(request: Request, data: dict[str, Any]) -> Envelope[dict[str, Any]]:
    return Envelope(data=data, meta=ResponseMeta(request_id=request.state.request_id))


@router.get("/knowledge-bases/{knowledge_base_id}/folders")
async def folders(
    knowledge_base_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    parent_id: Identifier | None = None,
    scope: Literal["active", "recycle_bin"] = "active",
    limit: Limit = 20,
    cursor: Cursor = None,
) -> Page[dict[str, Any]]:
    base = await domain.resource(
        session,
        identity,
        "knowledge_base",
        parse_id(knowledge_base_id),
        recycled=False if scope == "active" else None,
        write=False,
    )
    pagination = Pagination(
        request.app.state.tokens,
        {
            "resource": "folders",
            "actor": str(identity.user.id),
            "base": knowledge_base_id,
            "parent": parent_id,
            "scope": scope,
        },
    )
    query = select(Folder).where(
        Folder.knowledge_base_id == base.id,
        Folder.id > pagination.decode(cursor),
        Folder.deleted_at.is_(None) if scope == "active" else Folder.deleted_at.is_not(None),
    )
    if parent_id is not None:
        query = query.where(Folder.parent_id == parse_id(parent_id))
    rows = list((await session.scalars(query.order_by(Folder.id).limit(limit + 1))).all())
    return Page(
        data=[await domain.output(session, row) for row in rows[:limit]],
        meta=PageMeta(
            request_id=request.state.request_id,
            next_cursor=pagination.encode(rows[limit - 1].id) if len(rows) > limit else None,
        ),
    )


@router.post("/knowledge-bases/{knowledge_base_id}/folders", status_code=201)
async def create_folder(
    knowledge_base_id: Identifier,
    body: FolderCreate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: Key,
) -> Envelope[dict[str, Any]]:
    identity = await domain.write_guard(session, identity)
    result = await idempotent_create(
        session,
        identity,
        key,
        f"folder.create:{knowledge_base_id}",
        body,
        request.app.state.tokens,
        lambda: domain.create_folder(
            session, identity, parse_id(knowledge_base_id), body.model_dump()
        ),
    )
    return envelope(request, result)


@router.get("/folders/{folder_id}")
async def folder(
    folder_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    scope: Literal["active", "recycle_bin"] = "active",
) -> Envelope[dict[str, Any]]:
    row = await domain.resource(
        session,
        identity,
        "folder",
        parse_id(folder_id),
        recycled=scope == "recycle_bin",
        write=False,
    )
    return envelope(request, await domain.output(session, row))


@router.patch("/folders/{folder_id}")
async def update_folder(
    folder_id: Identifier,
    body: FolderUpdate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> Envelope[dict[str, Any]]:
    identity = await domain.write_guard(session, identity)
    return envelope(
        request,
        await domain.update_resource(
            session, identity, "folder", parse_id(folder_id), body.model_dump(exclude_unset=True)
        ),
    )


@router.patch("/documents/{document_id}")
async def update_document(
    document_id: Identifier,
    body: DocumentUpdate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> Envelope[dict[str, Any]]:
    identity = await domain.write_guard(session, identity)
    return envelope(
        request,
        await domain.update_resource(
            session,
            identity,
            "document",
            parse_id(document_id),
            body.model_dump(exclude_unset=True),
        ),
    )


def confirmation_endpoint(kind: str, verb: str) -> Any:
    async def endpoint(
        resource_id: Identifier,
        body: ResourceVersion,
        request: Request,
        session: SessionDependency,
        identity: Identity,
        key: Key,
    ) -> Envelope[dict[str, Any]]:
        identity = await domain.write_guard(session, identity)

        async def create() -> dict[str, Any]:
            if kind == "folder" and verb == "delete":
                root = await domain.resource(
                    session, identity, kind, parse_id(resource_id), recycled=False
                )
                domain.version(root, body.row_version)
                if len(await domain.members(session, root)) == 1:
                    return await domain.soft_delete(session, root)
            return await confirmation.create(
                session,
                identity,
                f"{kind}.{verb}",
                parse_id(resource_id),
                body.row_version,
                key,
                ttl_seconds=request.app.state.agent.config.pending_action_ttl_seconds,
            )

        result = await idempotent_create(
            session,
            identity,
            key,
            f"{kind}.{verb}:{resource_id}",
            body,
            request.app.state.tokens,
            create,
        )
        return envelope(request, result)

    endpoint.__name__ = f"request_{kind}_{verb}"
    return endpoint


def restore_endpoint(kind: str) -> Any:
    async def endpoint(
        resource_id: Identifier,
        body: RestoreInput,
        request: Request,
        session: SessionDependency,
        identity: Identity,
        key: Key,
    ) -> Envelope[dict[str, Any]]:
        identity = await domain.write_guard(session, identity)
        result = await idempotent_create(
            session,
            identity,
            key,
            f"{kind}.restore:{resource_id}",
            body,
            request.app.state.tokens,
            lambda: domain.restore(
                session, identity, kind, parse_id(resource_id), body.model_dump(exclude_unset=True)
            ),
        )
        return envelope(request, result)

    endpoint.__name__ = f"restore_{kind}"
    return endpoint


for _kind, _path in (
    ("knowledge_base", "knowledge-bases"),
    ("folder", "folders"),
    ("document", "documents"),
    ("conversation", "conversations"),
):
    router.add_api_route(
        f"/{_path}/{{resource_id}}/deletion-requests",
        confirmation_endpoint(_kind, "delete"),
        methods=["POST"],
    )
    if _kind != "conversation":
        router.add_api_route(
            f"/{_path}/{{resource_id}}/purge-requests",
            confirmation_endpoint(_kind, "purge"),
            methods=["POST"],
        )
        router.add_api_route(
            f"/{_path}/{{resource_id}}/restore", restore_endpoint(_kind), methods=["POST"]
        )


@router.post("/documents/{document_id}/processing/retry", status_code=202)
async def retry(
    document_id: Identifier,
    body: ResourceVersion,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: Key,
) -> Envelope[dict[str, Any]]:
    identity = await domain.write_guard(session, identity)

    async def create() -> dict[str, Any]:
        row = await domain.resource(
            session, identity, "document", parse_id(document_id), recycled=False
        )
        assert isinstance(row, Document)
        domain.version(row, body.row_version)
        return await domain.retry_processing(session, row)

    return envelope(
        request,
        await idempotent_create(
            session,
            identity,
            key,
            f"document.retry:{document_id}",
            body,
            request.app.state.tokens,
            create,
        ),
    )


@router.get("/pending-actions/{pending_action_id}")
async def pending(
    pending_action_id: Identifier, request: Request, session: SessionDependency, identity: Identity
) -> Envelope[dict[str, Any]]:
    action = await confirmation.get(session, identity, parse_id(pending_action_id))
    if action.bound_arguments["binding"]["permission_scope_version"] != scope_version(identity):
        raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
    result = confirmation.view(action)
    targets = list(
        (
            await session.scalars(
                select(PendingActionTarget)
                .where(PendingActionTarget.pending_action_id == action.id)
                .order_by(PendingActionTarget.ordinal)
            )
        ).all()
    )
    result["targets"] = [
        {
            "id": str(t.resource_id),
            "resource_type": t.resource_type,
            "display_name": t.display_name,
            "path": t.target_snapshot.get("path"),
        }
        for t in targets
    ]
    return envelope(request, result)


@router.post("/pending-actions/{pending_action_id}/responses")
async def respond(
    pending_action_id: Identifier,
    body: PendingResponse,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: Key,
) -> Envelope[dict[str, Any]]:
    action = await session.scalar(
        select(PendingAction).where(
            PendingAction.id == parse_id(pending_action_id),
            PendingAction.actor_user_id == identity.user.id,
        )
    )
    if action is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    if action.operation_name == "conversation.delete" and body.decision == "accept":
        conversation_id = int(action.bound_arguments["binding"]["arguments"]["resource_id"])
        run_ids = list(
            (
                await session.scalars(
                    select(AgentRun.id)
                    .where(AgentRun.conversation_id == conversation_id)
                    .order_by(AgentRun.id)
                )
            ).all()
        )
        for run_id in run_ids:
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": -run_id})
    if action.agent_run_id is not None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": -action.agent_run_id}
        )
    identity = await domain.write_guard(session, identity)
    storage = request.app.state.storage

    async def respond_with_import_copy() -> dict[str, Any]:
        copied: dict[str, Any] | None = None
        action_for_copy = await confirmation.get(session, identity, parse_id(pending_action_id))
        if (
            action_for_copy.operation_name == "document.generated_write"
            and body.decision == "accept"
            and action_for_copy.status == "pending"
        ):
            if storage is None:
                raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
            copied = await import_service.prepare_generated_copy(
                session, identity, action_for_copy, storage
            )
        try:
            return await confirmation.respond(
                session,
                identity,
                parse_id(pending_action_id),
                key,
                body.decision,
                body.selected_target_id,
                generated_copy=copied,
            )
        except BaseException:
            if copied is not None and storage is not None:
                try:
                    await import_service.cleanup_generated_copy(storage, copied)
                except BaseException:
                    pass
            raise

    result = await idempotent_create(
        session,
        identity,
        key,
        f"pending_action.respond:{pending_action_id}",
        body,
        request.app.state.tokens,
        respond_with_import_copy,
    )
    if result["status"] == "expired":
        # Keep the terminal expiry durable before returning its protocol error.
        await session.commit()
        raise APIError(ErrorCode.PENDING_ACTION_EXPIRED)
    return envelope(request, result)


@router.post("/documents/{document_id}/overwrite-requests", status_code=202)
async def overwrite_request(
    document_id: Identifier,
    request: Request,
    key: Key,
    identity: Annotated[CurrentIdentity, Depends(detached_identity)],
) -> Envelope[dict[str, Any]]:
    storage = request.app.state.storage
    if storage is None:
        raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
    async with request.app.state.sessions() as session, session.begin():
        await domain.resource(
            session, identity, "document", parse_id(document_id), recycled=False, write=False
        )
    files, fields = await receive_upload(
        request, storage, request.app.state.config.upload, allowed_fields={"row_version"}
    )
    if len(files) != 1 or set(fields) != {"row_version"}:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    try:
        expected = int(fields["row_version"])
        if expected < 1 or str(expected) != fields["row_version"]:
            raise ValueError
    except ValueError:
        raise APIError(ErrorCode.VALIDATION_ERROR) from None
    file = files[0]
    if file.error:
        raise APIError(file.error)
    async with request.app.state.sessions() as session, session.begin():
        identity = await domain.write_guard(session, identity)
        ttl = request.app.state.agent.config.pending_action_ttl_seconds
        staged = StorageObject(
            bucket_name=storage.bucket,
            object_key=file.key,
            object_type="original",
            size_bytes=file.size,
            content_type=file.mime_type,
            content_sha256=file.digest,
            status="staged",
            retention_until=datetime.now(UTC) + timedelta(seconds=ttl),
        )
        session.add(staged)
        await session.flush()
        result = await confirmation.create(
            session,
            identity,
            "document.overwrite",
            parse_id(document_id),
            expected,
            key,
            arguments={
                "staged_upload_id": str(staged.id),
                "file_name": file.filename,
                "file_extension": file.extension,
                "content_sha256": file.digest,
            },
            ttl_seconds=ttl,
        )
        action = await session.get(PendingAction, int(result["id"]))
        if action and action.bound_arguments["binding"]["arguments"]["arguments"][
            "staged_upload_id"
        ] == str(staged.id):
            staged.retention_until = action.expires_at
    return envelope(request, result)
