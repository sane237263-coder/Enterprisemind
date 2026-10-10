"""Strict request and response contracts for the Database MCP admin APIs."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from enterprise_mind.api.management_schemas import Identifier, InputModel, Version

SourceStatus = Literal["active", "disabled", "error"]
SnapshotStatus = Literal["building", "ready", "active", "failed", "retired"]
RelationType = Literal["table", "view"]
ConnectionTestClassification = Literal[
    "connected",
    "authentication_failed",
    "network_failed",
    "permission_failed",
    "schema_unavailable",
]


def _non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.strip():
        raise ValueError("must not be empty")
    return value


class DatabaseSourceCreate(InputModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    host: Annotated[str, Field(min_length=1, max_length=255)]
    port: Annotated[int, Field(strict=True, ge=1, le=65535)]
    database_name: Annotated[str, Field(min_length=1, max_length=200)]
    ssl_mode: Literal["disable", "require", "verify-ca", "verify-full"]
    secret_ref: Annotated[str, Field(min_length=1, max_length=500)]
    connection_options: dict[str, object] | None = None
    status: SourceStatus = "active"

    _name = field_validator("name", "host", "database_name", "ssl_mode", "secret_ref")(_non_empty)


class DatabaseSourceUpdate(InputModel):
    row_version: Version
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    host: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    port: Annotated[int, Field(strict=True, ge=1, le=65535)] | None = None
    database_name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    ssl_mode: Literal["disable", "require", "verify-ca", "verify-full"] | None = None
    secret_ref: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    connection_options: dict[str, object] | None = None
    status: SourceStatus | None = None

    _name = field_validator("name", "host", "database_name", "ssl_mode", "secret_ref")(_non_empty)

    @model_validator(mode="after")
    def check_patch(self) -> "DatabaseSourceUpdate":
        if self.model_fields_set == {"row_version"}:
            raise ValueError("empty update")
        if any(
            getattr(self, field) is None for field in self.model_fields_set - {"connection_options"}
        ):
            raise ValueError("source fields cannot be null")
        return self


class DatabaseSourceOutput(InputModel):
    id: str
    name: str
    host: str
    port: int
    database_name: str
    ssl_mode: str
    connection_options: dict[str, object] | None
    status: SourceStatus
    active_snapshot_id: str | None
    row_version: int
    configuration_version: int
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def utc_time(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class DatabaseConnectionTestOutput(InputModel):
    reachable: bool
    relation_count: int
    message: str
    classification: ConnectionTestClassification = "connected"


class SchemaRefreshOutput(InputModel):
    id: str
    database_source_id: str
    snapshot_no: int
    status: SnapshotStatus
    captured_at: datetime | None
    error_code: str | None
    error_message: str | None
    created_at: datetime

    @field_serializer("captured_at", "created_at")
    def utc_time(self, value: datetime | None) -> str | None:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


class SchemaColumnOutput(InputModel):
    id: str
    ordinal: int
    column_name: str
    data_type: str
    comment: str | None
    is_nullable: bool | None
    is_unique: bool


class SchemaConstraintOutput(InputModel):
    id: str
    constraint_name: str
    constraint_type: Literal["primary_key", "unique", "foreign_key"]
    column_names: list[str]
    referenced_schema_name: str | None
    referenced_relation_name: str | None
    referenced_column_names: list[str] | None


class SchemaRelationOutput(InputModel):
    id: str
    schema_name: str
    relation_name: str
    relation_type: RelationType
    comment: str | None
    columns: list[SchemaColumnOutput]
    constraints: list[SchemaConstraintOutput]


class SchemaCatalogOutput(InputModel):
    database_source_id: str
    schema_snapshot_id: str
    snapshot_no: int
    relations: list[SchemaRelationOutput]


class RelationPolicyUpdate(InputModel):
    schema_name: Annotated[str, Field(min_length=1, max_length=200)]
    relation_name: Annotated[str, Field(min_length=1, max_length=200)]
    enabled: bool
    department_ids: list[Identifier] = Field(default_factory=list, max_length=1000)
    row_version: Version | None = None

    @field_validator("schema_name", "relation_name")
    @classmethod
    def non_empty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("department_ids")
    @classmethod
    def unique_departments(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("department_ids must be unique")
        return values


class RelationPolicyOutput(InputModel):
    id: str
    database_source_id: str
    schema_name: str
    relation_name: str
    relation_type: RelationType | None
    enabled: bool
    department_ids: list[str]
    row_version: int
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def utc_time(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class AuthorizedColumnOutput(InputModel):
    column_name: str
    data_type: str
    comment: str | None
    is_nullable: bool | None = None
    is_unique: bool = False


class AuthorizedConstraintOutput(InputModel):
    constraint_type: Literal["primary_key", "unique", "foreign_key"]
    column_names: list[str]
    referenced_schema_name: str | None = None
    referenced_relation_name: str | None = None
    referenced_column_names: list[str] | None = None


class AuthorizedRelationOutput(InputModel):
    schema_name: str
    relation_name: str
    relation_type: RelationType
    comment: str | None
    columns: list[AuthorizedColumnOutput]
    constraints: list[AuthorizedConstraintOutput]


class AuthorizedSchemaOutput(InputModel):
    database_source_id: str
    schema_snapshot_id: str
    relations: list[AuthorizedRelationOutput]
