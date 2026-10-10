from typing import Literal

from pydantic import Field, model_validator

from enterprise_mind.api.management_schemas import (
    Identifier,
    InputModel,
    Name,
    Version,
    reject_nulls,
)


class FolderCreate(InputModel):
    name: Name
    parent_id: Identifier | None = None


class ResourceVersion(InputModel):
    row_version: Version


class FolderUpdate(ResourceVersion):
    name: Name | None = None
    parent_id: Identifier | None = None

    @model_validator(mode="after")
    def changes(self) -> "FolderUpdate":
        reject_nulls(self, {"parent_id"})
        return self


class DocumentUpdate(ResourceVersion):
    name: Name | None = None
    description: str | None = Field(default=None, max_length=10000)
    folder_id: Identifier | None = None

    @model_validator(mode="after")
    def changes(self) -> "DocumentUpdate":
        reject_nulls(self, {"description", "folder_id"})
        return self


class RestoreInput(ResourceVersion):
    name: Name | None = None
    parent_id: Identifier | None = None


class PendingResponse(InputModel):
    decision: Literal["accept", "reject"]
    selected_target_id: Identifier | None = None
