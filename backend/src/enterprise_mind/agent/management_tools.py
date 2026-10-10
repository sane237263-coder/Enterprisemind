"""Administrator tools reuse the same locked services as the management API."""

from typing import Any

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.api.management_schemas import (
    DepartmentCreate,
    DepartmentUpdate,
    Identifier,
    InputModel,
    Name,
    Role,
    Status,
    Username,
    UserUpdate,
    reject_nulls,
)
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.identity import management
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.security import normalize_name
from enterprise_mind.identity.service import CurrentIdentity


class OrganizationSelector(InputModel):
    id: Identifier | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def identifiable(self) -> "OrganizationSelector":
        if not self.id and not self.name:
            raise ValueError("resource selector required")
        return self


class OrganizationTarget(InputModel):
    target: OrganizationSelector


class UserChanges(InputModel):
    username: Username | None = None
    role: Role | None = None
    department_id: Identifier | None = None
    status: Status | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> "UserChanges":
        reject_nulls(self, {"department_id"})
        return self


class DepartmentChanges(InputModel):
    name: Name | None = None
    status: Status | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> "DepartmentChanges":
        reject_nulls(self, set())
        return self


class UserUpdateTool(OrganizationTarget):
    changes: UserChanges


class DepartmentUpdateTool(OrganizationTarget):
    changes: DepartmentChanges


SCHEMAS: dict[str, type[InputModel]] = {
    "user_update": UserUpdateTool,
    "department_create": DepartmentCreate,
    "department_update": DepartmentUpdateTool,
    "department_delete": OrganizationTarget,
}

DESCRIPTIONS = {
    "user_update": "管理员修改唯一用户的用户名、角色、部门或状态；不接受密码或凭据。",
    "department_create": "管理员创建部门，可指定名称和启用状态。",
    "department_update": "管理员修改唯一部门的名称或启用状态。",
    "department_delete": "管理员删除无用户、无知识库引用的部门；有引用时拒绝删除，无需二次确认。",
}


async def resolve(
    session: AsyncSession,
    identity: CurrentIdentity,
    kind: str,
    selector: OrganizationSelector,
) -> User | Department:
    AuthorizationService.require_admin(identity)
    model = User if kind == "user" else Department
    key = User.username_key if kind == "user" else Department.name_key
    query = select(model).execution_options(populate_existing=True)
    if selector.id:
        query = query.where(model.id == int(selector.id))
    if selector.name:
        query = query.where(
            key == normalize_name(selector.name, max_length=100 if kind == "user" else 200)
        )
    # Both names are globally unique after normalization. Never select a fuzzy first match.
    row = await session.scalar(query.with_for_update())
    if row is None:
        raise ValueError("request an exact authorized username, department name or identifier")
    assert isinstance(row, User | Department)
    return row


async def normalize(
    session: AsyncSession,
    identity: CurrentIdentity,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    identity = await management.admin_write(session, identity)
    body = SCHEMAS[name].model_validate(arguments)
    values = body.model_dump(mode="json", exclude_unset=True)
    if isinstance(body, DepartmentCreate):
        management.name_key(body.name)
        return values
    assert isinstance(body, OrganizationTarget)
    row = await resolve(session, identity, name.split("_")[0], body.target)
    values["target"] = {"id": str(row.id)}
    values["row_version"] = row.row_version
    return values


async def apply(
    session: AsyncSession,
    identity: CurrentIdentity,
    name: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    await management.admin_write(session, identity)
    if name == "department_create":
        return await management.create_department(session, DepartmentCreate.model_validate(values))
    identifier = int(values["target"]["id"])
    if name == "department_delete":
        await management.delete_department(session, identifier, values["row_version"])
        return {"id": str(identifier), "status": "deleted"}
    changes = {**values["changes"], "row_version": values["row_version"]}
    if name == "user_update":
        return (
            await management.update_user(session, identifier, UserUpdate.model_validate(changes))
        ).model_dump(mode="json")
    return (
        await management.update_department(
            session, identifier, DepartmentUpdate.model_validate(changes)
        )
    ).model_dump(mode="json")
