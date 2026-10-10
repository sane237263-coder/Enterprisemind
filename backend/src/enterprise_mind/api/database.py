"""Administrator APIs for the single Database MCP source and schema catalog."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from enterprise_mind.api.auth import Envelope, ResponseMeta, SessionDependency, current_identity
from enterprise_mind.api.database_schemas import (
    DatabaseConnectionTestOutput,
    DatabaseSourceCreate,
    DatabaseSourceOutput,
    DatabaseSourceUpdate,
    RelationPolicyOutput,
    RelationPolicyUpdate,
    SchemaCatalogOutput,
    SchemaRefreshOutput,
)
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.database import service
from enterprise_mind.identity.management import admin_write, idempotent_create
from enterprise_mind.identity.security import parse_id
from enterprise_mind.identity.service import CurrentIdentity

router = APIRouter(prefix="/api/v1/admin/database-mcp", tags=["database-mcp"])
Identity = Annotated[CurrentIdentity, Depends(current_identity)]
RefreshKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]
SourceKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]


def _meta(request: Request) -> ResponseMeta:
    return ResponseMeta(request_id=request.state.request_id)


@router.get("/source")
async def get_source(
    request: Request, session: SessionDependency, identity: Identity
) -> Envelope[DatabaseSourceOutput | None]:
    AuthorizationService.require_admin(identity)
    source = await service.get_source(session)
    return Envelope(data=service.source_output(source) if source else None, meta=_meta(request))


@router.post("/test-connection")
async def test_connection(
    request: Request, session: SessionDependency, identity: Identity
) -> Envelope[DatabaseConnectionTestOutput]:
    AuthorizationService.require_admin(identity)
    source = await service.require_source(session, active=True)
    client = getattr(request.app.state, "database_client", None)
    if client is None:
        raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE)
    catalog = await client.fetch_schema(source)
    return Envelope(
        data=DatabaseConnectionTestOutput(
            reachable=True,
            relation_count=len(catalog.relations),
            message="连接与只读账号检查通过；Schema 是否发布仍以刷新状态为准。",
            classification="connected",
        ),
        meta=_meta(request),
    )


@router.put("/source", status_code=201)
async def put_source(
    body: DatabaseSourceCreate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: SourceKey,
) -> Envelope[DatabaseSourceOutput]:
    identity = await admin_write(session, identity)

    async def create() -> dict[str, object]:
        return (await service.create_source(session, body)).model_dump(mode="json")

    result = await idempotent_create(
        session,
        identity,
        key,
        "database_source.create",
        body,
        request.app.state.tokens,
        create,
    )
    return Envelope(
        data=DatabaseSourceOutput.model_validate(result, strict=False),
        meta=_meta(request),
    )


@router.patch("/source")
async def patch_source(
    body: DatabaseSourceUpdate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> Envelope[DatabaseSourceOutput]:
    identity = await admin_write(session, identity)
    del identity
    return Envelope(data=await service.update_source(session, body), meta=_meta(request))


@router.post("/schema-refresh", status_code=202)
async def post_schema_refresh(
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: RefreshKey,
) -> Envelope[SchemaRefreshOutput]:
    identity = await admin_write(session, identity)
    return Envelope(
        data=await service.request_schema_refresh(
            session, actor_user_id=identity.user.id, idempotency_key=key
        ),
        meta=_meta(request),
    )


@router.get("/schema-refreshes/{schema_snapshot_id}")
async def get_schema_refresh(
    schema_snapshot_id: str,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> Envelope[SchemaRefreshOutput]:
    AuthorizationService.require_admin(identity)
    try:
        snapshot_id = parse_id(schema_snapshot_id)
    except ValueError:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND) from None
    return Envelope(data=await service.get_refresh(session, snapshot_id), meta=_meta(request))


@router.get("/schema-refresh/latest")
async def get_latest_schema_refresh(
    request: Request, session: SessionDependency, identity: Identity
) -> Envelope[SchemaRefreshOutput | None]:
    """Expose durable refresh state so an admin page can recover after reload."""

    AuthorizationService.require_admin(identity)
    return Envelope(data=await service.get_latest_refresh(session), meta=_meta(request))


@router.get("/schema")
async def get_schema(
    request: Request, session: SessionDependency, identity: Identity
) -> Envelope[SchemaCatalogOutput]:
    AuthorizationService.require_admin(identity)
    return Envelope(data=await service.active_catalog(session), meta=_meta(request))


@router.get("/relation-policies")
async def get_relation_policies(
    request: Request, session: SessionDependency, identity: Identity
) -> Envelope[list[RelationPolicyOutput]]:
    AuthorizationService.require_admin(identity)
    return Envelope(data=await service.list_relation_policies(session), meta=_meta(request))


@router.put("/relation-policies")
async def put_relation_policy(
    body: RelationPolicyUpdate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> Envelope[RelationPolicyOutput]:
    identity = await admin_write(session, identity)
    del identity
    return Envelope(data=await service.upsert_relation_policy(session, body), meta=_meta(request))


__all__ = ["router"]
