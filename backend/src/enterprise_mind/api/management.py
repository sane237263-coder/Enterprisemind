from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import select

from enterprise_mind.api.auth import (
    Envelope,
    ResponseMeta,
    SessionDependency,
    UserSummary,
    current_identity,
)
from enterprise_mind.api.management_schemas import (
    DepartmentCreate,
    DepartmentOutput,
    DepartmentUpdate,
    Identifier,
    KnowledgeBaseCreate,
    KnowledgeBaseOutput,
    KnowledgeBaseUpdate,
    PasswordReset,
    Status,
    UserCreate,
    UserUpdate,
)
from enterprise_mind.api.pagination import Page, PageMeta, Pagination
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.identity import management as service
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.security import BIGINT_MAX, parse_id
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.knowledge.models import KnowledgeBase

router = APIRouter(prefix="/api/v1", tags=["organization"])
Identity = Annotated[CurrentIdentity, Depends(current_identity)]
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=4096)]
Key = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$"),
]


def meta(request: Request) -> ResponseMeta:
    return ResponseMeta(request_id=request.state.request_id)


@router.get("/admin/users")
async def users(
    request: Request,
    session: SessionDependency,
    identity: Identity,
    status: Status | None = None,
    department_id: Identifier | None = None,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> Page[UserSummary]:
    AuthorizationService.require_admin(identity)
    pagination = Pagination(
        request.app.state.tokens,
        {
            "resource": "users",
            "actor": str(identity.user.id),
            "status": status,
            "department_id": department_id,
        },
    )
    query = select(User).where(User.id > pagination.decode(cursor))
    if status is not None:
        query = query.where(User.status == status)
    if department_id is not None:
        query = query.where(User.department_id == parse_id(department_id))
    rows = list((await session.scalars(query.order_by(User.id).limit(limit + 1))).all())
    return Page(
        data=[await service.user_output(session, row) for row in rows[:limit]],
        meta=PageMeta(
            request_id=request.state.request_id,
            next_cursor=pagination.encode(rows[limit - 1].id) if len(rows) > limit else None,
        ),
    )


@router.post("/admin/users", status_code=201)
async def create_user(
    body: UserCreate, request: Request, session: SessionDependency, identity: Identity, key: Key
) -> Envelope[UserSummary]:
    identity = await service.admin_write(session, identity)
    result = await service.idempotent_create(
        session,
        identity,
        key,
        "user.create",
        body,
        request.app.state.tokens,
        lambda: service.create_user(session, body, request.app.state.passwords),
    )
    return Envelope(data=UserSummary.model_validate(result), meta=meta(request))


@router.get("/admin/users/{user_id}")
async def user(
    user_id: Identifier, request: Request, session: SessionDependency, identity: Identity
) -> Envelope[UserSummary]:
    AuthorizationService.require_admin(identity)
    return Envelope(
        data=await service.user_output(session, await service.get_user(session, parse_id(user_id))),
        meta=meta(request),
    )


@router.patch("/admin/users/{user_id}")
async def update_user(
    user_id: Identifier,
    body: UserUpdate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> Envelope[UserSummary]:
    await service.admin_write(session, identity)
    return Envelope(
        data=await service.update_user(session, parse_id(user_id), body), meta=meta(request)
    )


@router.post("/admin/users/{user_id}/password-reset")
async def password_reset(
    user_id: Identifier,
    body: PasswordReset,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> Envelope[UserSummary]:
    await service.admin_write(session, identity)
    return Envelope(
        data=await service.reset_password(
            session, parse_id(user_id), body, request.app.state.passwords
        ),
        meta=meta(request),
    )


@router.get("/admin/departments")
async def departments(
    request: Request,
    session: SessionDependency,
    identity: Identity,
    status: Status | None = None,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> Page[DepartmentOutput]:
    AuthorizationService.require_admin(identity)
    pagination = Pagination(
        request.app.state.tokens,
        {"resource": "departments", "actor": str(identity.user.id), "status": status},
    )
    query = select(Department).where(Department.id > pagination.decode(cursor))
    if status is not None:
        query = query.where(Department.status == status)
    rows = list((await session.scalars(query.order_by(Department.id).limit(limit + 1))).all())
    return Page(
        data=[service.department_output(row) for row in rows[:limit]],
        meta=PageMeta(
            request_id=request.state.request_id,
            next_cursor=pagination.encode(rows[limit - 1].id) if len(rows) > limit else None,
        ),
    )


@router.post("/admin/departments", status_code=201)
async def create_department(
    body: DepartmentCreate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: Key,
) -> Envelope[DepartmentOutput]:
    identity = await service.admin_write(session, identity)
    result = await service.idempotent_create(
        session,
        identity,
        key,
        "department.create",
        body,
        request.app.state.tokens,
        lambda: service.create_department(session, body),
    )
    return Envelope(data=DepartmentOutput.model_validate(result), meta=meta(request))


@router.get("/admin/departments/{department_id}")
async def department(
    department_id: Identifier, request: Request, session: SessionDependency, identity: Identity
) -> Envelope[DepartmentOutput]:
    AuthorizationService.require_admin(identity)
    return Envelope(
        data=service.department_output(
            await service.get_department(session, parse_id(department_id))
        ),
        meta=meta(request),
    )


@router.patch("/admin/departments/{department_id}")
async def update_department(
    department_id: Identifier,
    body: DepartmentUpdate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> Envelope[DepartmentOutput]:
    await service.admin_write(session, identity)
    return Envelope(
        data=await service.update_department(session, parse_id(department_id), body),
        meta=meta(request),
    )


@router.delete("/admin/departments/{department_id}", status_code=204)
async def delete_department(
    department_id: Identifier,
    session: SessionDependency,
    identity: Identity,
    row_version: Annotated[int, Query(ge=1, le=BIGINT_MAX)],
) -> Response:
    await service.admin_write(session, identity)
    await service.delete_department(session, parse_id(department_id), row_version)
    return Response(status_code=204)


@router.get("/knowledge-bases")
async def knowledge_bases(
    request: Request,
    session: SessionDependency,
    identity: Identity,
    scope: Literal["active", "recycle_bin"] = "active",
    type: Literal["public", "department"] | None = None,
    department_id: Identifier | None = None,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> Page[KnowledgeBaseOutput]:
    pagination = Pagination(
        request.app.state.tokens,
        {
            "resource": "knowledge_bases",
            "scope": scope,
            "actor": str(identity.user.id),
            "type": type,
            "department_id": department_id,
        },
    )
    query = select(KnowledgeBase).where(
        AuthorizationService.knowledge_scope(identity), KnowledgeBase.id > pagination.decode(cursor)
    )
    if scope == "recycle_bin":
        from sqlalchemy import or_

        query = select(KnowledgeBase).where(
            KnowledgeBase.deleted_at.is_not(None), KnowledgeBase.id > pagination.decode(cursor)
        )
        if identity.user.role != "admin":
            query = query.where(
                or_(
                    KnowledgeBase.type == "public",
                    KnowledgeBase.department_id == identity.user.department_id,
                )
            )
    if type is not None:
        query = query.where(KnowledgeBase.type == type)
    if department_id is not None:
        query = query.where(KnowledgeBase.department_id == parse_id(department_id))
    rows = list((await session.scalars(query.order_by(KnowledgeBase.id).limit(limit + 1))).all())
    return Page(
        data=[service.knowledge_output(row) for row in rows[:limit]],
        meta=PageMeta(
            request_id=request.state.request_id,
            next_cursor=pagination.encode(rows[limit - 1].id) if len(rows) > limit else None,
        ),
    )


@router.post("/knowledge-bases", status_code=201)
async def create_knowledge_base(
    body: KnowledgeBaseCreate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: Key,
) -> Envelope[KnowledgeBaseOutput]:
    identity = await service.admin_write(session, identity)
    result = await service.idempotent_create(
        session,
        identity,
        key,
        "knowledge_base.create",
        body,
        request.app.state.tokens,
        lambda: service.create_knowledge_base(session, body),
    )
    return Envelope(data=KnowledgeBaseOutput.model_validate(result), meta=meta(request))


@router.get("/knowledge-bases/{knowledge_base_id}")
async def knowledge_base(
    knowledge_base_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    scope: Literal["active", "recycle_bin"] = "active",
) -> Envelope[KnowledgeBaseOutput]:
    from enterprise_mind.knowledge.lifecycle import resource

    base = await resource(
        session,
        identity,
        "knowledge_base",
        parse_id(knowledge_base_id),
        recycled=scope == "recycle_bin",
        write=False,
    )
    assert isinstance(base, KnowledgeBase)
    return Envelope(
        data=service.knowledge_output(base),
        meta=meta(request),
    )


@router.patch("/knowledge-bases/{knowledge_base_id}")
async def update_knowledge_base(
    knowledge_base_id: Identifier,
    body: KnowledgeBaseUpdate,
    request: Request,
    session: SessionDependency,
    identity: Identity,
) -> Envelope[KnowledgeBaseOutput]:
    identity = await service.admin_write(session, identity)
    return Envelope(
        data=await service.update_knowledge_base(
            session, identity, parse_id(knowledge_base_id), body
        ),
        meta=meta(request),
    )
