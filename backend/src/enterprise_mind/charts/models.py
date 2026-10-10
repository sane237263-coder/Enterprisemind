"""Immutable, authorization-neutral chart data contracts.

The chart renderer accepts only a bounded snapshot that an upstream service has
already authorized and aggregated.  It never accepts SQL, query parameters or
model supplied executable configuration.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

ChartType = Literal["bar", "line", "pie"]
ChartFormat = Literal["png", "pdf", "xlsx", "csv"]
ChartScope = Literal["current_page", "all_aggregated", "top_n"]
SortOrder = Literal["input", "ascending", "descending"]
DimensionKind = Literal["category", "time"]

CHART_TYPES = frozenset({"bar", "line", "pie"})
CHART_FORMATS = frozenset({"png", "pdf", "xlsx", "csv"})
CHART_SCOPES = frozenset({"current_page", "all_aggregated", "top_n"})
MAX_INPUT_ROWS = {"bar": 50, "line": 100, "pie": 20}
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
SQL_LIKE_RE = re.compile(
    r"(?:\bselect\b|\binsert\b|\bupdate\b|\bdelete\b|\bdrop\b|--|/\*|;)",
    re.IGNORECASE,
)
TEMPLATE_RE = re.compile(r"(?:\{\{|\}\}|\$\{|<script|javascript:|eval\s*\()", re.IGNORECASE)


class ChartValidationError(ValueError):
    """A deterministic, user-safe chart validation failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require_text(value: str, code: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChartValidationError(code, f"{field} must be non-empty text")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise ChartValidationError(code, f"{field} contains a control character")
    if TEMPLATE_RE.search(value):
        raise ChartValidationError(code, f"{field} contains executable template syntax")
    return value.strip()


def _require_ref(value: str, code: str, field: str) -> str:
    value = _require_text(value, code, field)
    if SQL_LIKE_RE.search(value):
        raise ChartValidationError(code, f"{field} must be an opaque reference")
    return value


def _parse_time(value: str) -> datetime | date:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ChartValidationError(
                "CHART_TIME_DIMENSION_INVALID", "time dimensions must be ISO dates or timestamps"
            ) from error


def _decimal(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, bool):
        raise ChartValidationError("CHART_VALUE_INVALID", "boolean values are not metrics")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ChartValidationError(
            "CHART_VALUE_INVALID", "metric values must be decimal"
        ) from error
    if not result.is_finite():
        raise ChartValidationError("CHART_VALUE_NONFINITE", "NaN and Infinity are not metrics")
    return result


@dataclass(frozen=True)
class ChartPoint:
    """One already-authorized dimension/metric pair in a frozen snapshot."""

    dimension: str
    value: Decimal | None

    def __post_init__(self) -> None:
        dimension = _require_text(self.dimension, "CHART_DIMENSION_INVALID", "dimension")
        object.__setattr__(self, "dimension", dimension)
        if self.value is not None:
            object.__setattr__(self, "value", _decimal(self.value))


@dataclass(frozen=True)
class ChartSpec:
    """Validated chart intent and source metadata without executable query text."""

    chart_type: ChartType
    title: str
    data_ref: str
    dimension_column: str
    metric_column: str
    statistic: str
    scope: ChartScope
    scope_label: str
    source_refs: tuple[str, ...]
    queried_at: str
    revision: str
    unit: str = ""
    sort_order: SortOrder = "input"
    dimension_kind: DimensionKind = "category"
    aggregate_complete: bool = False
    top_n: int | None = None
    display_precision: int | None = None
    content_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_refs", tuple(self.source_refs))


@dataclass(frozen=True)
class ChartSnapshot:
    """A bounded, hash-bound input accepted by all four renderers."""

    spec: ChartSpec
    rows: tuple[ChartPoint, ...]

    @classmethod
    def freeze(
        cls, spec: ChartSpec, rows: tuple[ChartPoint, ...] | list[ChartPoint]
    ) -> ChartSnapshot:
        normalized = tuple(rows)
        validate_chart(spec, normalized)
        ordered = _ordered_rows(spec, normalized)
        payload = _canonical_payload(spec, ordered)
        content_hash = hashlib.sha256(payload).hexdigest()
        return cls(replace(spec, content_hash=content_hash), ordered)


def _ordered_rows(spec: ChartSpec, rows: tuple[ChartPoint, ...]) -> tuple[ChartPoint, ...]:
    if spec.sort_order == "input":
        return rows
    keyed = [
        (_parse_time(row.dimension) if spec.dimension_kind == "time" else row.dimension, row)
        for row in rows
    ]
    keyed.sort(key=lambda item: item[0], reverse=spec.sort_order == "descending")
    return tuple(row for _, row in keyed)


def _canonical_payload(spec: ChartSpec, rows: tuple[ChartPoint, ...]) -> bytes:
    value = {
        "chart_type": spec.chart_type,
        "title": spec.title,
        "data_ref": spec.data_ref,
        "dimension_column": spec.dimension_column,
        "metric_column": spec.metric_column,
        "statistic": spec.statistic,
        "scope": spec.scope,
        "scope_label": spec.scope_label,
        "source_refs": list(spec.source_refs),
        "queried_at": spec.queried_at,
        "revision": spec.revision,
        "unit": spec.unit,
        "sort_order": spec.sort_order,
        "dimension_kind": spec.dimension_kind,
        "aggregate_complete": spec.aggregate_complete,
        "top_n": spec.top_n,
        "display_precision": spec.display_precision,
        "rows": [
            {
                "dimension": row.dimension,
                "value": None if row.value is None else format(row.value, "f"),
            }
            for row in rows
        ],
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def validate_chart(spec: ChartSpec, rows: tuple[ChartPoint, ...] | list[ChartPoint]) -> None:
    """Validate limits, semantics, precision and security before rendering."""

    if spec.chart_type not in CHART_TYPES:
        raise ChartValidationError("CHART_TYPE_INVALID", "chart_type is not supported")
    if spec.scope not in CHART_SCOPES:
        raise ChartValidationError("CHART_SCOPE_INVALID", "scope is not supported")
    if spec.sort_order not in {"input", "ascending", "descending"}:
        raise ChartValidationError("CHART_SORT_INVALID", "sort_order is not supported")
    if spec.dimension_kind not in {"category", "time"}:
        raise ChartValidationError(
            "CHART_DIMENSION_KIND_INVALID", "dimension_kind is not supported"
        )
    _require_text(spec.title, "CHART_TITLE_INVALID", "title")
    _require_ref(spec.data_ref, "CHART_DATA_REF_INVALID", "data_ref")
    for name, value in (
        ("dimension_column", spec.dimension_column),
        ("metric_column", spec.metric_column),
    ):
        if not IDENTIFIER_RE.fullmatch(value):
            raise ChartValidationError(
                "CHART_COLUMN_INVALID", f"{name} must be a validated column identity"
            )
    _require_text(spec.statistic, "CHART_STATISTIC_INVALID", "statistic")
    if SQL_LIKE_RE.search(spec.statistic):
        raise ChartValidationError("CHART_STATISTIC_INVALID", "statistic must not contain SQL")
    _require_text(spec.scope_label, "CHART_SCOPE_LABEL_INVALID", "scope_label")
    _require_text(spec.queried_at, "CHART_TIME_INVALID", "queried_at")
    try:
        parsed_time = datetime.fromisoformat(spec.queried_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ChartValidationError(
            "CHART_TIME_INVALID", "queried_at must be an ISO timestamp"
        ) from error
    if parsed_time.tzinfo is None:
        raise ChartValidationError("CHART_TIME_INVALID", "queried_at must include a timezone")
    _require_ref(spec.revision, "CHART_REVISION_INVALID", "revision")
    if spec.unit:
        _require_text(spec.unit, "CHART_UNIT_INVALID", "unit")
    if not spec.source_refs:
        raise ChartValidationError(
            "CHART_SOURCE_REQUIRED", "at least one source reference is required"
        )
    for source_ref in spec.source_refs:
        _require_ref(source_ref, "CHART_SOURCE_INVALID", "source_ref")
    if spec.display_precision is not None and not 0 <= spec.display_precision <= 12:
        raise ChartValidationError("CHART_PRECISION_INVALID", "display_precision must be 0..12")
    if spec.scope == "current_page":
        if spec.aggregate_complete:
            raise ChartValidationError(
                "CHART_SCOPE_INVALID", "current_page cannot claim complete aggregation"
            )
        if "当前页" not in spec.scope_label and "current page" not in spec.scope_label.casefold():
            raise ChartValidationError(
                "CHART_SCOPE_LABEL_INVALID", "current_page must be visible in scope_label"
            )
    elif spec.scope == "all_aggregated":
        if not spec.aggregate_complete:
            raise ChartValidationError(
                "CHART_AGGREGATION_INCOMPLETE", "all_aggregated requires aggregate_complete"
            )
        if "全部" not in spec.scope_label and "all" not in spec.scope_label.casefold():
            raise ChartValidationError(
                "CHART_SCOPE_LABEL_INVALID", "all_aggregated must be visible in scope_label"
            )
    else:
        if not isinstance(spec.top_n, int) or isinstance(spec.top_n, bool) or spec.top_n <= 0:
            raise ChartValidationError("CHART_TOP_N_INVALID", "top_n must be a positive integer")
        if "前" not in spec.scope_label and "top" not in spec.scope_label.casefold():
            raise ChartValidationError(
                "CHART_SCOPE_LABEL_INVALID", "top_n must be visible in scope_label"
            )
    if spec.chart_type == "line" and spec.dimension_kind == "time" and spec.sort_order == "input":
        raise ChartValidationError(
            "CHART_TIME_ORDER_REQUIRED", "time line charts require explicit sorting"
        )

    point_rows = tuple(rows)
    if len(point_rows) > MAX_INPUT_ROWS[spec.chart_type]:
        raise ChartValidationError("CHART_BUDGET_EXCEEDED", "chart point budget exceeded")
    if spec.scope == "top_n" and spec.top_n is not None and len(point_rows) > spec.top_n:
        raise ChartValidationError("CHART_TOP_N_EXCEEDED", "snapshot contains more rows than top_n")
    dimensions = [row.dimension for row in point_rows]
    if len(dimensions) != len(set(dimensions)):
        raise ChartValidationError(
            "CHART_DUPLICATE_DIMENSION", "dimensions must be pre-aggregated and unique"
        )
    if spec.dimension_kind == "time":
        for dimension in dimensions:
            _parse_time(dimension)
    if spec.chart_type == "pie":
        if any(row.value is None for row in point_rows):
            raise ChartValidationError(
                "CHART_NULL_PIE_VALUE", "pie charts require a value for every category"
            )
        values = [row.value for row in point_rows if row.value is not None]
        if any(value < 0 for value in values):
            raise ChartValidationError(
                "CHART_NEGATIVE_PIE_VALUE", "pie values must be non-negative"
            )
        if not values or sum(values, Decimal(0)) <= 0:
            raise ChartValidationError(
                "CHART_PIE_TOTAL_ZERO", "pie total must be greater than zero"
            )
    for row in point_rows:
        if (
            row.value is not None
            and len(row.value.as_tuple().digits) > 15
            and spec.display_precision is None
        ):
            raise ChartValidationError(
                "CHART_PRECISION_POLICY_REQUIRED",
                "large or high precision values require an explicit display_precision",
            )
    if spec.content_hash is not None and not HEX64_RE.fullmatch(spec.content_hash):
        raise ChartValidationError("CHART_HASH_INVALID", "content_hash must be SHA-256")


def canonical_chart_bytes(snapshot: ChartSnapshot) -> bytes:
    """Expose the exact bytes used to bind the snapshot hash for evidence/tests."""

    return _canonical_payload(snapshot.spec, snapshot.rows)
