"""Fixed, non-secret navigation actions; never tools or arbitrary URLs."""

from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.agent.management_tools import OrganizationSelector, resolve
from enterprise_mind.agent.models import Message
from enterprise_mind.api.errors import APIError
from enterprise_mind.api.management_schemas import (
    Identifier,
    InputModel,
    Role,
    Status,
    Username,
    Version,
)
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.identity import management
from enterprise_mind.identity.models import User
from enterprise_mind.identity.service import CurrentIdentity


class UserPrefill(InputModel):
    username: Username | None = None
    role: Role | None = None
    department_id: Identifier | None = None
    status: Status | None = None


class CreateUserAction(InputModel):
    action: Literal["open_admin_user_create"]
    prefill: UserPrefill = Field(default_factory=UserPrefill)


class ResetPasswordAction(InputModel):
    action: Literal["open_admin_user_password_reset"]
    target: OrganizationSelector


ClientActionRequest = Annotated[
    CreateUserAction | ResetPasswordAction, Field(discriminator="action")
]


class CreateUserPart(CreateUserAction):
    type: Literal["client_action"] = "client_action"


class ResetPasswordPart(InputModel):
    type: Literal["client_action"] = "client_action"
    action: Literal["open_admin_user_password_reset"]
    user_id: Identifier
    display_name: Username
    row_version: Version


ACTION_PART: TypeAdapter[CreateUserPart | ResetPasswordPart] = TypeAdapter(
    Annotated[CreateUserPart | ResetPasswordPart, Field(discriminator="action")]
)


async def check_prefill(session: AsyncSession, prefill: UserPrefill) -> None:
    if prefill.username is not None:
        management.name_key(prefill.username, 100)
    await management.department_exists(
        session, int(prefill.department_id) if prefill.department_id else None
    )


async def prepare(
    session: AsyncSession, identity: CurrentIdentity, request: ClientActionRequest
) -> dict[str, Any]:
    AuthorizationService.require_admin(identity)
    if isinstance(request, CreateUserAction):
        await check_prefill(session, request.prefill)
        return CreateUserPart(**request.model_dump()).model_dump(mode="json", exclude_none=True)
    user = await resolve(session, identity, "user", request.target)
    assert isinstance(user, User)
    return ResetPasswordPart(
        action=request.action,
        user_id=str(user.id),
        display_name=user.username,
        row_version=user.row_version,
    ).model_dump(mode="json")


async def validate(
    session: AsyncSession, identity: CurrentIdentity, part: dict[str, Any]
) -> dict[str, Any]:
    AuthorizationService.require_admin(identity)
    body = ACTION_PART.validate_python(part)
    if isinstance(body, CreateUserPart):
        await check_prefill(session, body.prefill)
    else:
        user = await management.get_user(session, int(body.user_id))
        management.check_version(user, body.row_version)
        if user.username != body.display_name:
            raise ValueError("client action target changed")
    return body.model_dump(mode="json", exclude_none=True)


async def output(
    session: AsyncSession, identity: CurrentIdentity, message: Message, part: dict[str, Any]
) -> dict[str, Any] | None:
    # A proposed action is private until the answer transaction completes.
    if (
        message.role != "assistant"
        or message.status != "completed"
        or identity.user.role != "admin"
    ):
        return None
    try:
        safe = ACTION_PART.validate_python(part).model_dump(mode="json", exclude_none=True)
    except ValidationError:
        return None
    try:
        await validate(session, identity, safe)
    except (APIError, ValueError):
        return {"type": "client_action", "action": safe["action"], "available": False}
    return {**safe, "available": True}


def explanation(part: dict[str, Any]) -> str:
    if part["action"] == "open_admin_user_create":
        return (
            "请打开用户创建页面，核对信息并在专用表单中设置密码后提交。"
            "当前尚未创建用户。请勿在对话中发送密码。"
        )
    return (
        "请打开该用户的密码重置页面，在专用表单中设置新密码后提交。"
        "当前尚未重置密码。请勿在对话中发送密码。"
    )
