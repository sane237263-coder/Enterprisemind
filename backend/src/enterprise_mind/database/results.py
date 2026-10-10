"""Normalization and in-memory budgeting for Database MCP rows.

The normalizer is deliberately independent of SQLAlchemy/asyncpg.  The MCP
adapter can pass column metadata and rows from either driver while keeping all
PostgreSQL-specific values out of the model context.  It never truncates a
cell or a row: a row that would cross the byte budget is left out entirely.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from .contracts import QueryResult

RESULT_BUDGET_BYTES = 1024 * 1024


class ResultTooLargeError(ValueError):
    """The complete server-owned page cannot fit the model result budget."""


DEFAULT_RESULT_ROWS = 100


@dataclass(frozen=True)
class ResultColumn:
    """Temporary column metadata returned by the enterprise database."""

    name: str
    data_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("result column name is invalid")
        if not isinstance(self.data_type, str) or not self.data_type:
            raise ValueError("result column data_type is invalid")
        try:
            self.name.encode("utf-8")
            self.data_type.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("result column metadata must be valid UTF-8") from exc


def _column_parts(column: Any) -> tuple[str, str]:
    if isinstance(column, ResultColumn):
        return column.name, column.data_type
    if isinstance(column, Mapping):
        name = column.get("name", column.get("column_name"))
        data_type = column.get("data_type", column.get("type", ""))
    elif isinstance(column, Sequence) and not isinstance(column, str | bytes | bytearray):
        if len(column) < 2:
            raise ValueError("result column tuple requires name and data_type")
        name, data_type = column[0], column[1]
    else:
        name = getattr(column, "name", getattr(column, "column_name", None))
        data_type = getattr(column, "data_type", getattr(column, "type", ""))
    if not isinstance(name, str) or not name:
        raise ValueError("result column name is invalid")
    if not isinstance(data_type, str):
        data_type = str(data_type)
    if not data_type:
        raise ValueError("result column data_type is invalid")
    return name, data_type


def _base_type(data_type: str) -> str:
    """Return a conservative lower-case PostgreSQL type family."""

    value = data_type.strip().lower()
    # Drop only type modifiers; the type family remains visible for conversion.
    if "(" in value:
        value = value.split("(", 1)[0].strip()
    if value.endswith("[]"):
        return value
    return value


def is_bytea_type(data_type: str) -> bool:
    value = _base_type(data_type)
    if value.endswith("[]"):
        value = value[:-2].strip()
    return value in {"bytea", "pg_catalog.bytea"} or value.endswith(".bytea")


def _normalize_json(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        # bytea is never a model value.  Nested bytea values in JSON/arrays are
        # represented as null rather than exposing their encoded contents.
        return None
    if isinstance(value, Mapping):
        return {str(key): _normalize_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_normalize_json(item) for item in value]
    # Driver-specific scalar types should not be allowed to leak their repr;
    # a string is the least surprising JSON-safe representation.
    return str(value)


def normalize_value(value: Any, data_type: str | None = None) -> Any:
    """Convert one PostgreSQL value to a JSON-safe model-context value."""

    if data_type is not None and is_bytea_type(data_type):
        return None
    if data_type is not None:
        family = _base_type(data_type)
        if family.endswith("[]") and isinstance(value, list | tuple):
            element_type = family[:-2].strip()
            return [normalize_value(item, element_type) for item in value]
        if (
            family
            in {
                "bigint",
                "int8",
                "pg_catalog.int8",
                "numeric",
                "decimal",
                "pg_catalog.numeric",
            }
            and isinstance(value, int | Decimal)
            and not isinstance(value, bool)
        ):
            return str(value) if isinstance(value, int) else format(value, "f")
    return _normalize_json(value)


def _row_mapping(row: Any, columns: tuple[ResultColumn, ...]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return {
            column.name: normalize_value(row.get(column.name), column.data_type)
            for column in columns
        }
    if isinstance(row, Sequence) and not isinstance(row, str | bytes | bytearray):
        if len(row) != len(columns):
            raise ValueError("result row and column counts differ")
        return {
            column.name: normalize_value(value, column.data_type)
            for column, value in zip(columns, row, strict=True)
        }
    # asyncpg.Record is Mapping-like in supported versions; unknown row
    # objects fail closed rather than exposing driver repr text.
    raise ValueError("result row has unsupported shape")


def _serialized_size(value: Any) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("result row is not JSON serializable") from exc


def _budget_payload(
    columns: tuple[ResultColumn, ...],
    rows: Sequence[Mapping[str, Any]],
    *,
    row_count: int,
    has_more: bool,
    referenced_relations: tuple[str, ...],
    elapsed_ms: float,
) -> dict[str, Any]:
    """Build the complete model-facing envelope used for byte accounting."""

    return {
        "columns": [{"name": column.name, "data_type": column.data_type} for column in columns],
        "rows": rows,
        "row_count": row_count,
        "has_more": has_more,
        "referenced_relations": referenced_relations,
        "elapsed_ms": elapsed_ms,
    }


def normalize_query_result(
    columns: Iterable[Any],
    rows: Iterable[Any],
    *,
    page_size: int = DEFAULT_RESULT_ROWS,
    referenced_relations: Iterable[str] = (),
    elapsed_ms: float = 0.0,
    max_bytes: int = RESULT_BUDGET_BYTES,
) -> QueryResult:
    """Normalize at most ``page_size`` complete rows and report ``has_more``.

    Callers should provide up to ``page_size + 1`` rows.  If the iterator has
    more rows than that, the extra row is used only to set ``has_more`` and is
    never returned.  If the complete page exceeds ``max_bytes``, an explicit
    ``ResultTooLargeError`` is raised; no rows are silently removed.
    """

    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise ValueError("page_size must be between 1 and 100")
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= RESULT_BUDGET_BYTES
    ):
        raise ValueError("max_bytes must be between 1 and 1 MiB")
    if (
        isinstance(elapsed_ms, bool)
        or not isinstance(elapsed_ms, int | float)
        or not math.isfinite(elapsed_ms)
        or elapsed_ms < 0
    ):
        raise ValueError("elapsed_ms must be non-negative")

    raw_columns = tuple(_column_parts(column) for column in columns)
    raw_names = tuple(name for name, _ in raw_columns)
    if len(raw_names) != len(set(raw_names)):
        raise ValueError("result columns must have unique names")
    visible_columns = tuple(
        ResultColumn(name, data_type)
        for name, data_type in raw_columns
        if not is_bytea_type(data_type)
    )
    # Keep source positions so a bytea column can be dropped without shifting
    # sequence-based driver rows.
    visible_indexes = tuple(
        index for index, (_, data_type) in enumerate(raw_columns) if not is_bytea_type(data_type)
    )

    relation_names = tuple(str(item) for item in referenced_relations)
    elapsed_value = float(elapsed_ms)

    # The empty envelope is still model-visible metadata.  Reject it before
    # consuming the driver iterator when even the columns and control fields
    # cannot fit; returning an over-budget empty result would make the byte
    # limit meaningless.
    empty_payload = _budget_payload(
        visible_columns,
        (),
        row_count=0,
        has_more=False,
        referenced_relations=relation_names,
        elapsed_ms=elapsed_value,
    )
    if _serialized_size(empty_payload) > max_bytes:
        raise ResultTooLargeError("result metadata exceeds byte budget")

    iterator = iter(rows)
    accepted: list[dict[str, Any]] = []
    has_more = False
    for index, raw_row in enumerate(iterator):
        if index >= page_size:
            has_more = True
            break
        if isinstance(raw_row, Mapping):
            normalized = {
                column.name: normalize_value(raw_row.get(column.name), column.data_type)
                for column in visible_columns
            }
        elif isinstance(raw_row, Sequence) and not isinstance(raw_row, str | bytes | bytearray):
            if len(raw_row) != len(raw_columns):
                raise ValueError("result row and column counts differ")
            normalized = {
                column.name: normalize_value(raw_row[source_index], column.data_type)
                for column, source_index in zip(visible_columns, visible_indexes, strict=True)
            }
        else:
            raise ValueError("result row has unsupported shape")
        candidate_rows = [*accepted, normalized]
        # Check the larger of the two final boolean representations.  JSON
        # ``false`` is one byte longer than ``true``, so a candidate that fits
        # with has_more=false remains within budget if a later row changes the
        # final result to has_more=true.  Never return a partial page: doing so
        # would make the next server-owned offset skip rows that were removed
        # only for model-context budgeting.
        if (
            _serialized_size(
                _budget_payload(
                    visible_columns,
                    candidate_rows,
                    row_count=len(candidate_rows),
                    has_more=False,
                    referenced_relations=relation_names,
                    elapsed_ms=elapsed_value,
                )
            )
            > max_bytes
        ):
            raise ResultTooLargeError("complete result page exceeds byte budget")
        accepted.append(normalized)

    return QueryResult(
        columns=visible_columns,
        rows=tuple(accepted),
        row_count=len(accepted),
        has_more=has_more,
        referenced_relations=relation_names,
        elapsed_ms=elapsed_value,
    )


# Short aliases used by adapters that treat this as a result policy.
normalize_results = normalize_query_result
normalize_database_value = normalize_value


__all__ = [
    "DEFAULT_RESULT_ROWS",
    "RESULT_BUDGET_BYTES",
    "ResultColumn",
    "ResultTooLargeError",
    "is_bytea_type",
    "normalize_database_value",
    "normalize_query_result",
    "normalize_results",
    "normalize_value",
]
