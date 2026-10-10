"""Request-local contracts shared by the SQL guard and Database MCP adapter.

These objects deliberately are not persistence models.  A schema catalog is the
authorized projection for one query invocation and generated/guarded queries
are process-local values.  In particular, SQL, bind values, and rows are kept
out of their representations so an accidental exception or debug display does
not turn them into a log payload.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Any

type JSONScalar = str | int | float | bool | None
type RelationKey = tuple[str, str]


def _tuple(value: Sequence[Any] | None, *, field: str) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, str | bytes | bytearray):
        raise TypeError(f"{field} must be a sequence, not text")
    return tuple(value)


def _require_text(value: str, *, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{field} must be non-empty text")
    # Surrogate code points cannot be sent to PostgreSQL or serialized as UTF-8.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8 text") from exc
    return value


def _validate_scalar(value: Any, *, field: str) -> JSONScalar:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{field} must be a finite JSON number")
        return value
    raise TypeError(f"{field} must be a JSON scalar")


def _column_display_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        candidate = value.get("name", value.get("column_name"))
        return candidate if isinstance(candidate, str) else None
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        candidate = value[0] if value else None
        return candidate if isinstance(candidate, str) else None
    candidate = getattr(value, "name", getattr(value, "column_name", None))
    return candidate if isinstance(candidate, str) else None


@dataclass(frozen=True)
class SchemaColumn:
    """One field from the active external PostgreSQL schema snapshot."""

    name: str
    data_type: str
    comment: str | None = None
    ordinal: int = 0
    is_nullable: bool | None = None
    is_unique: bool = False

    def __post_init__(self) -> None:
        _require_text(self.name, field="column name")
        _require_text(self.data_type, field="column data_type")
        if self.comment is not None:
            _require_text(self.comment, field="column comment", allow_empty=True)
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool) or self.ordinal < 1:
            raise ValueError("column ordinal must be a positive integer")
        if self.is_nullable is not None and not isinstance(self.is_nullable, bool):
            raise TypeError("column is_nullable must be boolean or None")
        if not isinstance(self.is_unique, bool):
            raise TypeError("column is_unique must be boolean")

    @property
    def column_name(self) -> str:
        """Database snapshot naming alias."""

        return self.name


@dataclass(frozen=True)
class SchemaConstraint:
    """A primary-key or foreign-key constraint from a schema snapshot."""

    name: str
    constraint_type: str
    column_names: tuple[str, ...] = ()
    referenced_schema_name: str | None = None
    referenced_relation_name: str | None = None
    referenced_column_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.name, field="constraint name")
        if self.constraint_type not in {"primary_key", "unique", "foreign_key"}:
            raise ValueError("constraint_type must be primary_key, unique or foreign_key")
        columns = tuple(
            _require_text(item, field="constraint column name")
            for item in _tuple(self.column_names, field="column_names")
        )
        if not columns:
            raise ValueError("constraint must contain at least one column")
        if len(set(columns)) != len(columns):
            raise ValueError("constraint columns must be unique")
        object.__setattr__(self, "column_names", columns)

        refs = tuple(
            _require_text(item, field="referenced column name")
            for item in _tuple(self.referenced_column_names, field="referenced_column_names")
        )
        object.__setattr__(self, "referenced_column_names", refs)
        if self.constraint_type in {"primary_key", "unique"}:
            if (
                any(
                    value is not None
                    for value in (
                        self.referenced_schema_name,
                        self.referenced_relation_name,
                    )
                )
                or refs
            ):
                raise ValueError("primary_key and unique constraints cannot have references")
        else:
            if self.referenced_schema_name is None or self.referenced_relation_name is None:
                raise ValueError("foreign_key requires a referenced relation")
            _require_text(self.referenced_schema_name, field="referenced schema name")
            _require_text(self.referenced_relation_name, field="referenced relation name")
            if len(refs) != len(columns):
                raise ValueError("foreign-key column lists must have equal length")

    @property
    def constraint_name(self) -> str:
        """Database snapshot naming alias."""

        return self.name


@dataclass(frozen=True)
class SchemaRelation:
    """An authorized table or view and its current columns/constraints."""

    schema_name: str
    relation_name: str
    relation_type: str = "table"
    columns: tuple[SchemaColumn, ...] = ()
    comment: str | None = None
    constraints: tuple[SchemaConstraint, ...] = ()
    # These fields are internal snapshot identity/signature data.  They are
    # intentionally optional so old synthetic fixtures and already persisted
    # 0010 snapshots remain readable while new catalog fetches populate them.
    relation_oid: int | None = None
    relation_signature: str | None = None
    view_definition_signature: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.schema_name, field="schema name")
        _require_text(self.relation_name, field="relation name")
        if self.relation_type not in {"table", "view"}:
            raise ValueError("relation_type must be table or view")
        if self.comment is not None:
            _require_text(self.comment, field="relation comment", allow_empty=True)
        if self.relation_oid is not None and (
            not isinstance(self.relation_oid, int)
            or isinstance(self.relation_oid, bool)
            or self.relation_oid < 0
        ):
            raise ValueError("relation_oid must be a non-negative integer")
        for value, field in (
            (self.relation_signature, "relation signature"),
            (self.view_definition_signature, "view definition signature"),
        ):
            if value is not None:
                _require_text(value, field=field)
                if len(value) > 128:
                    raise ValueError(f"{field} is too long")
        columns = tuple(_tuple(self.columns, field="columns"))
        if not all(isinstance(item, SchemaColumn) for item in columns):
            raise TypeError("columns must contain SchemaColumn values")
        names = [item.name for item in columns]
        if len(names) != len(set(names)):
            raise ValueError("relation columns must be unique")
        object.__setattr__(self, "columns", columns)
        constraints = tuple(_tuple(self.constraints, field="constraints"))
        if not all(isinstance(item, SchemaConstraint) for item in constraints):
            raise TypeError("constraints must contain SchemaConstraint values")
        constraint_names = [item.name for item in constraints]
        if len(constraint_names) != len(set(constraint_names)):
            raise ValueError("relation constraints must be unique")
        object.__setattr__(self, "constraints", constraints)

    @property
    def key(self) -> RelationKey:
        return (self.schema_name, self.relation_name)

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.relation_name}"

    @property
    def quoted_qualified_name(self) -> str:
        """Canonical SQL identifier form, safe for display and set comparison."""

        def quote(value: str) -> str:
            return '"' + value.replace('"', '""') + '"'

        return f"{quote(self.schema_name)}.{quote(self.relation_name)}"

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)


@dataclass(frozen=True)
class SchemaCatalog:
    """The current authorized relation projection used by one query."""

    relations: tuple[SchemaRelation, ...] = ()

    def __post_init__(self) -> None:
        relations = tuple(_tuple(self.relations, field="relations"))
        if not all(isinstance(item, SchemaRelation) for item in relations):
            raise TypeError("relations must contain SchemaRelation values")
        keys = [item.key for item in relations]
        if len(keys) != len(set(keys)):
            raise ValueError("catalog relations must be unique")
        object.__setattr__(self, "relations", relations)

    @property
    def relation_map(self) -> Mapping[RelationKey, SchemaRelation]:
        return {relation.key: relation for relation in self.relations}

    def get(self, schema_name: str, relation_name: str) -> SchemaRelation | None:
        return self.relation_map.get((schema_name, relation_name))

    def __contains__(self, key: RelationKey) -> bool:
        return key in self.relation_map


def relation_signature(relation: SchemaRelation) -> str:
    """Return the internal shape/identity digest for one external relation.

    The digest deliberately contains the PostgreSQL object identity and the
    view-definition digest, but never the view definition itself.  Comments
    are excluded because they are descriptive, untrusted metadata; columns,
    nullability, keys and foreign-key targets are included because they affect
    query generation and authorization continuity.
    """

    payload = {
        "relation_oid": relation.relation_oid,
        "relation_type": relation.relation_type,
        "view_definition_signature": relation.view_definition_signature,
        "columns": [
            [column.ordinal, column.name, column.data_type, column.is_nullable]
            for column in relation.columns
        ],
        "constraints": sorted(
            [
                [
                    constraint.constraint_type,
                    list(constraint.column_names),
                    constraint.referenced_schema_name,
                    constraint.referenced_relation_name,
                    list(constraint.referenced_column_names),
                ]
                for constraint in relation.constraints
            ],
            key=lambda item: json.dumps(item, ensure_ascii=True, separators=(",", ":")),
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()


@dataclass(frozen=True, repr=False)
class GeneratedQuery:
    """Untrusted, process-local SQL generator output."""

    sql: str
    parameters: tuple[JSONScalar, ...] = ()
    query_kind: str = "detail"
    conditions_summary: str = ""

    def __post_init__(self) -> None:
        _require_text(self.sql, field="generated sql")
        if self.query_kind not in {"detail", "aggregate"}:
            raise ValueError("query_kind must be detail or aggregate")
        if not isinstance(self.conditions_summary, str):
            raise TypeError("conditions_summary must be text")
        params = tuple(
            _validate_scalar(item, field="query parameter")
            for item in _tuple(self.parameters, field="parameters")
        )
        object.__setattr__(self, "parameters", params)

    def __repr__(self) -> str:
        return (
            "GeneratedQuery(sql=<redacted>, parameters=<redacted>, "
            f"query_kind={self.query_kind!r}, conditions_summary=<redacted>)"
        )


@dataclass(frozen=True, repr=False)
class GuardedQuery:
    """AST-validated SQL plus server pagination and MCP replay input.

    ``sql`` is the server-paginated statement.  ``validated_query`` is the
    canonical, unpaginated single SELECT that the MCP server must parse again.
    ``validated_parameters`` are the values belonging to that statement;
    ``parameters`` additionally contains server-owned LIMIT/OFFSET values.
    """

    sql: str
    parameters: tuple[JSONScalar, ...]
    referenced_relations: tuple[str, ...]
    page_size: int
    page_index: int
    validated_query: str = ""
    validated_parameters: tuple[JSONScalar, ...] = ()
    # A model-authored business LIMIT is distinct from server transport
    # pagination.  Callers can reject it for an unbounded user request while
    # preserving explicit first-N semantics.
    has_business_limit: bool = False
    # A user-requested bounded export cap. It is process-local MCP input,
    # not ordinary query pagination and is ignored by execute_query.
    export_limit: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.sql, field="guarded sql")
        _require_text(self.validated_query, field="validated query", allow_empty=True)
        if not isinstance(self.page_size, int) or isinstance(self.page_size, bool):
            raise TypeError("page_size must be an integer")
        if not isinstance(self.page_index, int) or isinstance(self.page_index, bool):
            raise TypeError("page_index must be an integer")
        if self.page_size < 1 or self.page_size > 100:
            raise ValueError("page_size must be between 1 and 100")
        if self.page_index < 0:
            raise ValueError("page_index must be non-negative")
        relations = tuple(
            _require_text(item, field="referenced relation")
            for item in _tuple(self.referenced_relations, field="referenced_relations")
        )
        if len(relations) != len(set(relations)):
            raise ValueError("referenced_relations must be unique")
        object.__setattr__(self, "referenced_relations", relations)
        params = tuple(
            _validate_scalar(item, field="guarded parameter")
            for item in _tuple(self.parameters, field="parameters")
        )
        validated_params = tuple(
            _validate_scalar(item, field="validated parameter")
            for item in _tuple(self.validated_parameters, field="validated_parameters")
        )
        object.__setattr__(self, "parameters", params)
        object.__setattr__(self, "validated_parameters", validated_params)
        if not isinstance(self.has_business_limit, bool):
            raise TypeError("has_business_limit must be a boolean")
        if self.export_limit is not None and (
            isinstance(self.export_limit, bool)
            or not isinstance(self.export_limit, int)
            or not 1 <= self.export_limit <= 10_000
        ):
            raise ValueError("export_limit must be between 1 and 10000")

    @property
    def original_query(self) -> str:
        """Compatibility name for the MCP re-parse input."""

        return self.validated_query

    @property
    def original_parameters(self) -> tuple[JSONScalar, ...]:
        return self.validated_parameters

    def __repr__(self) -> str:
        return (
            "GuardedQuery(sql=<redacted>, parameters=<redacted>, "
            f"referenced_relations={self.referenced_relations!r}, "
            f"page_size={self.page_size!r}, page_index={self.page_index!r}, "
            f"has_business_limit={self.has_business_limit!r}, "
            f"export_limit={self.export_limit!r}, "
            "validated_query=<redacted>, validated_parameters=<redacted>)"
        )


@dataclass(frozen=True, repr=False)
class QueryResult:
    """Request-local, already budgeted database result.

    Rows are intentionally represented as an opaque tuple.  The result
    normalizer owns conversion of PostgreSQL-specific values and omission of
    bytea before this object is passed to an LLM.
    """

    columns: tuple[Any, ...]
    rows: tuple[Any, ...]
    row_count: int
    has_more: bool
    referenced_relations: tuple[str, ...]
    elapsed_ms: float

    def __post_init__(self) -> None:
        columns = tuple(_tuple(self.columns, field="columns"))
        rows = tuple(_tuple(self.rows, field="rows"))
        column_names = tuple(name for name in (_column_display_name(column) for column in columns))
        if any(name is None or not name for name in column_names):
            raise ValueError("result column name is invalid")
        if len(column_names) != len(set(column_names)):
            raise ValueError("result columns must have unique names")
        relations = tuple(
            _require_text(item, field="referenced relation")
            for item in _tuple(self.referenced_relations, field="referenced_relations")
        )
        if len(relations) != len(set(relations)):
            raise ValueError("referenced_relations must be unique")
        if (
            not isinstance(self.row_count, int)
            or isinstance(self.row_count, bool)
            or self.row_count < 0
        ):
            raise ValueError("row_count must be a non-negative integer")
        if self.row_count != len(rows):
            raise ValueError("row_count must equal the number of returned rows")
        if not isinstance(self.has_more, bool):
            raise TypeError("has_more must be boolean")
        if (
            not isinstance(self.elapsed_ms, int | float)
            or isinstance(self.elapsed_ms, bool)
            or not isfinite(self.elapsed_ms)
            or self.elapsed_ms < 0
        ):
            raise ValueError("elapsed_ms must be non-negative")
        object.__setattr__(self, "columns", columns)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "referenced_relations", relations)

    def __repr__(self) -> str:
        return (
            "QueryResult(columns=<redacted>, rows=<redacted>, "
            f"row_count={self.row_count!r}, has_more={self.has_more!r}, "
            f"referenced_relations={self.referenced_relations!r}, elapsed_ms={self.elapsed_ms!r})"
        )


__all__ = [
    "GeneratedQuery",
    "GuardedQuery",
    "JSONScalar",
    "QueryResult",
    "RelationKey",
    "relation_signature",
    "SchemaCatalog",
    "SchemaColumn",
    "SchemaConstraint",
    "SchemaRelation",
]
