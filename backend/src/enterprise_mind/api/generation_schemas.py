from typing import Any, Literal

from pydantic import Field, model_validator

from enterprise_mind.api.management_schemas import Identifier, InputModel, Name, Version


class DraftCreate(InputModel):
    title: str = Field(min_length=1, max_length=300)
    body_markdown: str = Field(min_length=1, max_length=10_000_000)
    tables: list[dict[str, Any]] = Field(default_factory=list, max_length=32)


class DraftUpdate(InputModel):
    expected_revision: Version
    title: str | None = Field(default=None, max_length=300)
    body_markdown: str | None = Field(default=None, max_length=10_000_000)
    tables: list[dict[str, Any]] | None = Field(default=None, max_length=32)


class RenderTaskCreate(InputModel):
    expected_revision: Version
    format: Literal["docx", "pdf", "md", "xlsx"]
    file_name: str | None = Field(default=None, max_length=500)


class DatabaseExportTaskCreate(InputModel):
    scope: Literal["current_page", "all_matching", "first_n"]
    format: Literal["xlsx", "csv"] = "xlsx"
    query_id: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    # This is an assertion of the user-level semantic LIMIT already present in
    # the query plan; the export worker never truncates an unbounded result.
    row_limit: int | None = Field(default=None, ge=1, le=10_000)
    result_id: str | None = Field(
        default=None, min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )
    file_name: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_scope(self) -> "DatabaseExportTaskCreate":
        if self.scope == "current_page" and self.result_id is None:
            raise ValueError("current_page export requires result_id")
        if self.scope != "current_page" and self.result_id is not None:
            raise ValueError("result_id is only valid for current_page export")
        if self.scope == "first_n" and self.row_limit is None:
            raise ValueError("first_n export requires row_limit")
        if self.scope != "first_n" and self.row_limit is not None:
            raise ValueError("row_limit is only valid for first_n export")
        return self


class GeneratedFileImportPreviewCreate(InputModel):
    knowledge_base_id: Identifier
    folder_id: Identifier | None = None
    name: Name
    description: str | None = Field(default=None, max_length=10000)
    conflict_action: Literal["reject", "overwrite"] = "reject"


__all__ = [
    "DatabaseExportTaskCreate",
    "DraftCreate",
    "DraftUpdate",
    "GeneratedFileImportPreviewCreate",
    "RenderTaskCreate",
]
