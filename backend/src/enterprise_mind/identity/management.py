import hmac
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from enterprise_mind.api.auth import UserSummary
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.api.management_schemas import (
    DepartmentCreate,
    DepartmentOutput,
    DepartmentUpdate,
    InputModel,
    KnowledgeBaseCreate,
    KnowledgeBaseOutput,
    KnowledgeBaseUpdate,
    PasswordReset,
    UserCreate,
    UserUpdate,
)
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.security import (
    BIGINT_MAX,
    Passwords,
    Tokens,
    normalize_name,
    parse_id,
)
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.infrastructure.models import IdempotencyRecord
from enterprise_mind.knowledge.models import KnowledgeBase

# Serializes the small V1 organization mutation surface across API workers.
# Authentication is refreshed after acquiring it, before taking target row locks.
ORGANIZATION_LOCK = 4617078203315146318


async def admin_write(session: AsyncSession, identity: CurrentIdentity) -> CurrentIdentity:
    AuthorizationService.require_admin(identity)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ORGANIZATION_LOCK})
    current = await AuthorizationService.revalidate(session, identity)
    AuthorizationService.require_admin(current)
    return current


def name_key(value: str, max_length: int = 200) -> str:
    try:
        return normalize_name(value, max_length=max_length)
    except ValueError:
        raise APIError(ErrorCode.VALIDATION_ERROR) from None


def check_version(entity: User | Department | KnowledgeBase, version: int) -> None:
    if entity.row_version != version or version == BIGINT_MAX:
        raise APIError(ErrorCode.ROW_VERSION_CONFLICT)


def bump(entity: User | Department | KnowledgeBase) -> None:
    entity.row_version += 1
    entity.updated_at = datetime.now(UTC)


async def flush(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise APIError(ErrorCode.NAME_CONFLICT) from None
        raise


async def department_exists(session: AsyncSession, department_id: int | None) -> None:
    if department_id is not None and await session.get(Department, department_id) is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)


async def get_user(session: AsyncSession, user_id: int, *, lock: bool = False) -> User:
    query = select(User).where(User.id == user_id).execution_options(populate_existing=True)
    user = (await session.scalars(query.with_for_update() if lock else query)).one_or_none()
    if user is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    return user


async def get_department(
    session: AsyncSession, department_id: int, *, lock: bool = False
) -> Department:
    query = (
        select(Department)
        .where(Department.id == department_id)
        .execution_options(populate_existing=True)
    )
    department = (await session.scalars(query.with_for_update() if lock else query)).one_or_none()
    if department is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    return department


async def get_knowledge_base(
    session: AsyncSession, identity: CurrentIdentity, resource_id: int, *, lock: bool = False
) -> KnowledgeBase:
    query = (
        select(KnowledgeBase)
        .where(KnowledgeBase.id == resource_id, AuthorizationService.knowledge_scope(identity))
        .execution_options(populate_existing=True)
    )
    resource = (await session.scalars(query.with_for_update() if lock else query)).one_or_none()
    if resource is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    return resource


async def user_output(session: AsyncSession, user: User) -> UserSummary:
    department = (
        await session.get(Department, user.department_id)
        if user.department_id is not None
        else None
    )
    return UserSummary.from_identity(CurrentIdentity(user, department))


def department_output(department: Department) -> DepartmentOutput:
    return DepartmentOutput.model_validate(
        {
            "id": str(department.id),
            "name": department.name,
            "status": department.status,
            "row_version": department.row_version,
            "created_at": department.created_at,
            "updated_at": department.updated_at,
        }
    )


def knowledge_output(resource: KnowledgeBase) -> KnowledgeBaseOutput:
    return KnowledgeBaseOutput.model_validate(
        {
            "id": str(resource.id),
            "name": resource.name,
            "type": resource.type,
            "description": resource.description,
            "department_id": str(resource.department_id)
            if resource.department_id is not None
            else None,
            "status": "recycle_bin" if resource.deleted_at else "active",
            "deleted_at": resource.deleted_at,
            "purge_after": resource.purge_after,
            "deletion_batch_id": str(resource.deletion_batch_id)
            if resource.deletion_batch_id
            else None,
            "row_version": resource.row_version,
            "created_at": resource.created_at,
            "updated_at": resource.updated_at,
        }
    )


async def hash_password(passwords: Passwords, password: SecretStr) -> str:
    try:
        return await run_in_threadpool(passwords.hash, password)
    except ValueError:
        raise APIError(ErrorCode.VALIDATION_ERROR) from None


async def create_user(
    session: AsyncSession, body: UserCreate, passwords: Passwords
) -> dict[str, Any]:
    department_id = parse_id(body.department_id) if body.department_id else None
    if body.role == "user" and department_id is None:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    await department_exists(session, department_id)
    user = User(
        username=body.username,
        username_key=name_key(body.username, 100),
        password_hash=await hash_password(passwords, body.password),
        role=body.role,
        department_id=department_id,
        status=body.status,
    )
    session.add(user)
    await flush(session)
    return (await user_output(session, user)).model_dump(mode="json")


async def update_user(session: AsyncSession, user_id: int, body: UserUpdate) -> UserSummary:
    user = await get_user(session, user_id, lock=True)
    check_version(user, body.row_version)
    role = body.role or user.role
    department_id = user.department_id
    if "department_id" in body.model_fields_set:
        department_id = parse_id(body.department_id) if body.department_id else None
    if role == "user" and department_id is None:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    await department_exists(session, department_id)
    if body.username is not None:
        user.username_key = name_key(body.username, 100)
        user.username = body.username
    user.role, user.department_id = role, department_id
    if body.status is not None:
        user.status = body.status
    bump(user)
    await flush(session)
    return await user_output(session, user)


async def reset_password(
    session: AsyncSession, user_id: int, body: PasswordReset, passwords: Passwords
) -> UserSummary:
    user = await get_user(session, user_id, lock=True)
    check_version(user, body.row_version)
    if user.credential_version == BIGINT_MAX:
        raise APIError(ErrorCode.ROW_VERSION_CONFLICT)
    user.password_hash = await hash_password(passwords, body.new_password)
    user.credential_version += 1
    bump(user)
    await flush(session)
    return await user_output(session, user)


async def create_department(session: AsyncSession, body: DepartmentCreate) -> dict[str, Any]:
    department = Department(name=body.name, name_key=name_key(body.name), status=body.status)
    session.add(department)
    await flush(session)
    return department_output(department).model_dump(mode="json")


async def update_department(
    session: AsyncSession, department_id: int, body: DepartmentUpdate
) -> DepartmentOutput:
    department = await get_department(session, department_id, lock=True)
    check_version(department, body.row_version)
    if body.name is not None:
        department.name_key, department.name = name_key(body.name), body.name
    if body.status is not None:
        department.status = body.status
    bump(department)
    await flush(session)
    return department_output(department)


async def delete_department(session: AsyncSession, department_id: int, version: int) -> None:
    department = await get_department(session, department_id, lock=True)
    check_version(department, version)
    users = await session.scalar(
        select(User.id).where(User.department_id == department_id).limit(1)
    )
    bases = await session.scalar(
        select(KnowledgeBase.id).where(KnowledgeBase.department_id == department_id).limit(1)
    )
    if users is not None or bases is not None:
        raise APIError(ErrorCode.DEPARTMENT_IN_USE)
    await session.delete(department)
    try:
        await session.flush()
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23503":
            raise APIError(ErrorCode.DEPARTMENT_IN_USE) from None
        raise


async def create_knowledge_base(session: AsyncSession, body: KnowledgeBaseCreate) -> dict[str, Any]:
    department_id = parse_id(body.department_id) if body.department_id else None
    if (body.type == "department") != (department_id is not None):
        raise APIError(ErrorCode.VALIDATION_ERROR)
    await department_exists(session, department_id)
    resource = KnowledgeBase(
        name=body.name,
        name_key=name_key(body.name),
        type=body.type,
        department_id=department_id,
        description=body.description,
    )
    session.add(resource)
    await flush(session)
    return knowledge_output(resource).model_dump(mode="json")


async def update_knowledge_base(
    session: AsyncSession, identity: CurrentIdentity, resource_id: int, body: KnowledgeBaseUpdate
) -> KnowledgeBaseOutput:
    resource = await get_knowledge_base(session, identity, resource_id, lock=True)
    check_version(resource, body.row_version)
    if "department_id" in body.model_fields_set:
        if resource.type != "department" or body.department_id is None:
            raise APIError(ErrorCode.VALIDATION_ERROR)
        department_id = parse_id(body.department_id)
        await department_exists(session, department_id)
        resource.department_id = department_id
    if body.name is not None:
        resource.name_key, resource.name = name_key(body.name), body.name
    if "description" in body.model_fields_set:
        resource.description = body.description
    bump(resource)
    await flush(session)
    return knowledge_output(resource)


async def idempotent_create(
    session: AsyncSession,
    identity: CurrentIdentity,
    key: str,
    operation: str,
    body: InputModel,
    tokens: Tokens,
    create: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    # Whitelisted typed input is hashed in memory; password values are never persisted.
    values = body.model_dump()
    for field, value in values.items():
        if isinstance(value, SecretStr):
            values[field] = value.get_secret_value()
    fingerprint = tokens.digest(
        "creation", json.dumps(values, sort_keys=True, ensure_ascii=True).encode()
    )
    record = (
        await session.scalars(
            select(IdempotencyRecord).where(
                IdempotencyRecord.actor_user_id == identity.user.id,
                IdempotencyRecord.idempotency_key == key,
            )
        )
    ).one_or_none()
    if record is not None:
        if record.operation_name != operation or not hmac.compare_digest(
            record.request_fingerprint, fingerprint
        ):
            raise APIError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
        return record.response_data
    result = await create()
    session.add(
        IdempotencyRecord(
            actor_user_id=identity.user.id,
            idempotency_key=key,
            operation_name=operation,
            request_fingerprint=fingerprint,
            resource_id=parse_id(result["id"]),
            response_data=result,
        )
    )
    await session.flush()
    return result
