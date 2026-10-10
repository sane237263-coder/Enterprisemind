from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    model_validator,
)

from enterprise_mind.identity.security import BIGINT_MAX, parse_id


def valid_id(value: str) -> str:
    parse_id(value)
    return value


Identifier = Annotated[str, AfterValidator(valid_id)]
Version = Annotated[int, Field(strict=True, ge=1, le=BIGINT_MAX)]
Name = Annotated[str, Field(min_length=1, max_length=200)]
Username = Annotated[str, Field(min_length=1, max_length=100)]
Status = Literal["active", "disabled"]
Role = Literal["admin", "user"]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class UserCreate(InputModel):
    username: Username
    password: SecretStr = Field(min_length=1, max_length=1024)
    role: Role
    department_id: Identifier | None = None
    status: Status = "active"


class UserUpdate(InputModel):
    row_version: Version
    username: Username | None = None
    role: Role | None = None
    department_id: Identifier | None = None
    status: Status | None = None

    @model_validator(mode="after")
    def check_patch(self) -> "UserUpdate":
        reject_nulls(self, {"department_id"})
        return self


class PasswordReset(InputModel):
    row_version: Version
    new_password: SecretStr = Field(min_length=1, max_length=1024)


class DepartmentCreate(InputModel):
    name: Name
    status: Status = "active"


class DepartmentUpdate(InputModel):
    row_version: Version
    name: Name | None = None
    status: Status | None = None

    @model_validator(mode="after")
    def check_patch(self) -> "DepartmentUpdate":
        reject_nulls(self, set())
        return self


class KnowledgeBaseCreate(InputModel):
    name: Name
    type: Literal["public", "department"]
    description: str | None = None
    department_id: Identifier | None = None


class KnowledgeBaseUpdate(InputModel):
    row_version: Version
    name: Name | None = None
    description: str | None = None
    department_id: Identifier | None = None

    @model_validator(mode="after")
    def check_patch(self) -> "KnowledgeBaseUpdate":
        reject_nulls(self, {"description"})
        return self


def reject_nulls(model: InputModel, nullable: set[str]) -> None:
    changed = model.model_fields_set - {"row_version"}
    if not changed or any(
        getattr(model, name) is None and name not in nullable for name in changed
    ):
        raise ValueError("invalid update")


class TimedOutput(BaseModel):
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def utc_time(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class DepartmentOutput(TimedOutput):
    id: str
    name: str
    status: Status
    row_version: int


class KnowledgeBaseOutput(TimedOutput):
    deleted_at: datetime | None = None
    purge_after: datetime | None = None
    deletion_batch_id: str | None = None
    id: str
    name: str
    type: Literal["public", "department"]
    description: str | None
    department_id: str | None
    status: Literal["active", "recycle_bin"]
    row_version: int
