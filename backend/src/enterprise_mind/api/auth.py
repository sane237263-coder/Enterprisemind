from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.identity.service import CurrentIdentity, authenticate, login

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
bearer = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    username: str = Field(min_length=1, max_length=100)
    # Bound verification input; creation uses the configured password policy.
    password: SecretStr = Field(min_length=1, max_length=1024)


class DepartmentSummary(BaseModel):
    id: str
    name: str
    status: Literal["active", "disabled"]


class UserSummary(BaseModel):
    id: str
    username: str
    role: Literal["admin", "user"]
    department_id: str | None
    department: DepartmentSummary | None
    status: Literal["active", "disabled"]
    row_version: int
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def utc_time(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @classmethod
    def from_identity(cls, identity: CurrentIdentity) -> "UserSummary":
        user, department = identity.user, identity.department
        return cls.model_validate(
            {
                "id": str(user.id),
                "username": user.username,
                "role": user.role,
                "department_id": str(user.department_id)
                if user.department_id is not None
                else None,
                "department": {
                    "id": str(department.id),
                    "name": department.name,
                    "status": department.status,
                }
                if department
                else None,
                "status": user.status,
                "row_version": user.row_version,
                "created_at": user.created_at,
                "updated_at": user.updated_at,
            }
        )


class ResponseMeta(BaseModel):
    request_id: str


class Envelope[T](BaseModel):
    data: T
    meta: ResponseMeta


class LoginData(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserSummary


async def database_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessions() as session:
        async with session.begin():
            yield session


SessionDependency = Annotated[AsyncSession, Depends(database_session, scope="function")]


async def current_identity(
    request: Request,
    session: SessionDependency,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> CurrentIdentity:
    if credentials is None:
        raise APIError(ErrorCode.AUTH_REQUIRED)
    return await authenticate(session, credentials.credentials, request.app.state.tokens)


@router.post("/login")
async def login_endpoint(
    body: LoginRequest, request: Request, session: SessionDependency
) -> Envelope[LoginData]:
    identity = await login(session, body.username, body.password, request.app.state.passwords)
    tokens = request.app.state.tokens
    return Envelope(
        data=LoginData(
            access_token=tokens.issue(identity.user.id, identity.user.credential_version),
            expires_in=tokens.ttl_seconds,
            user=UserSummary.from_identity(identity),
        ),
        meta=ResponseMeta(request_id=request.state.request_id),
    )


@router.get("/me")
async def me_endpoint(
    request: Request, identity: Annotated[CurrentIdentity, Depends(current_identity)]
) -> Envelope[UserSummary]:
    return Envelope(
        data=UserSummary.from_identity(identity),
        meta=ResponseMeta(request_id=request.state.request_id),
    )
