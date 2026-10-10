from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.security import Passwords, Tokens, normalize_name, parse_id


@dataclass(frozen=True)
class CurrentIdentity:
    user: User
    department: Department | None
    authenticated_credential_version: int | None = None


def state_error(identity: CurrentIdentity) -> ErrorCode | None:
    if identity.user.status != "active":
        return ErrorCode.ACCOUNT_DISABLED
    if identity.user.role == "user" and (
        identity.department is None or identity.department.status != "active"
    ):
        return ErrorCode.DEPARTMENT_DISABLED
    return None


async def login(
    session: AsyncSession, username: str, password: SecretStr, passwords: Passwords
) -> CurrentIdentity:
    try:
        username_key = normalize_name(username, max_length=100)
    except ValueError:
        # Invalid names take the same hash path as missing users.
        username_key = ""
    row = (
        await session.execute(
            select(User, Department)
            .outerjoin(Department, User.department_id == Department.id)
            .where(User.username_key == username_key)
        )
    ).one_or_none()
    valid = await run_in_threadpool(
        passwords.verify, password, row[0].password_hash if row else None
    )
    if row is None or not valid:
        raise APIError(ErrorCode.AUTH_INVALID_CREDENTIALS)
    identity = CurrentIdentity(row[0], row[1])
    if state_error(identity):
        raise APIError(ErrorCode.AUTH_INVALID_CREDENTIALS)
    return identity


async def authenticate(session: AsyncSession, token: str, tokens: Tokens) -> CurrentIdentity:
    try:
        claims = tokens.decode(token)
    except ValueError:
        raise APIError(ErrorCode.AUTH_REQUIRED) from None
    row = (
        await session.execute(
            select(User, Department)
            .outerjoin(Department, User.department_id == Department.id)
            .where(User.id == parse_id(claims.sub))
        )
    ).one_or_none()
    if row is None or row[0].credential_version != claims.credential_version:
        raise APIError(ErrorCode.AUTH_REQUIRED)
    identity = CurrentIdentity(row[0], row[1], claims.credential_version)
    if error := state_error(identity):
        raise APIError(error)
    return identity
