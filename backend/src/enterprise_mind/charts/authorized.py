"""Build chart snapshots only from an already guarded database answer.

The renderer is intentionally independent from authorization and SQL.  This
adapter is the narrow handoff for the real Agent/database path: the caller
must pass the request-local ``DatabaseAnswer`` returned by the database
gateway after its pre/post authorization checks.  Scope completeness and
source metadata are derived here from that answer; neither is accepted as a
browser or model assertion.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

from .models import (
    ChartPoint,
    ChartScope,
    ChartSnapshot,
    ChartSpec,
    ChartType,
    DimensionKind,
    SortOrder,
)


def freeze_database_answer(
    answer: Any,
    *,
    chart_type: ChartType,
    title: str,
    query_id: str,
    result_id: str | None,
    owner_user_id: int,
    conversation_id: int,
    query_context: Any,
    page: Any | None = None,
    scope: ChartScope,
    dimension_column: str,
    metric_column: str,
    statistic: str,
    dimension_kind: str = "category",
    sort_order: str = "input",
    top_n: int | None = None,
    unit: str = "",
    display_precision: int | None = None,
) -> ChartSnapshot:
    """Freeze a chart from a guarded, request-local database answer.

    ``answer`` is deliberately duck-typed so this module does not create a
    dependency from chart rendering to the database package.  Production
    callers pass ``DatabaseAnswer`` from ``DatabaseQueryService``; tests may
    use a small structural fake.  The function never accepts SQL, rows,
    source references, or an ``aggregate_complete`` flag from the caller.
    """

    prepared = getattr(answer, "prepared", None)
    binding = getattr(prepared, "binding", None)
    result = getattr(answer, "result", None)
    guarded = getattr(answer, "guarded_query", None)
    if prepared is None or binding is None or result is None or guarded is None:
        raise _error("CHART_DATABASE_ANSWER_INVALID", "answer is not a guarded database result")
    guarded_relations = tuple(getattr(guarded, "referenced_relations", ()))
    result_relations = tuple(getattr(result, "referenced_relations", ()))
    if not guarded_relations or guarded_relations != result_relations:
        raise _error(
            "CHART_DATABASE_BINDING_INVALID",
            "guarded query and result relations do not match",
        )
    if not isinstance(query_id, str) or not query_id.strip():
        raise _error("CHART_QUERY_REF_INVALID", "query_id is required")
    _verify_query_context(
        answer,
        query_context,
        query_id=query_id,
        owner_user_id=owner_user_id,
        conversation_id=conversation_id,
    )
    if scope == "current_page" and not result_id:
        raise _error("CHART_RESULT_REF_REQUIRED", "current_page requires result_id")
    if scope == "current_page":
        _verify_current_page(
            answer,
            query_context,
            page,
            query_id=query_id,
            result_id=cast(str, result_id),
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
        )

    query_kind = getattr(answer, "query_kind", None)
    has_more = getattr(result, "has_more", None)
    complete = False
    if scope == "all_aggregated":
        if query_kind != "aggregate" or has_more is not False:
            raise _error(
                "CHART_AGGREGATION_INCOMPLETE",
                "all_aggregated requires a complete aggregate result",
            )
        complete = True
    elif scope == "top_n":
        if query_kind != "aggregate" or has_more is not False:
            raise _error(
                "CHART_TOP_N_INCOMPLETE",
                "top_n requires a complete aggregate result",
            )
        if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n <= 0:
            raise _error("CHART_TOP_N_INVALID", "top_n must be positive")
    elif scope != "current_page":
        raise _error("CHART_SCOPE_INVALID", "unsupported chart scope")

    rows = tuple(_chart_points(result, dimension_column, metric_column))
    source_id = _safe_id(getattr(binding, "database_source_id", None), "source")
    snapshot_id = _safe_id(getattr(binding, "schema_snapshot_id", None), "snapshot")
    source_version = _safe_id(getattr(binding, "source_version", None), "source-version")
    permission = str(getattr(binding, "permission_fingerprint", ""))
    if not permission:
        raise _error("CHART_PERMISSION_BINDING_INVALID", "permission fingerprint is required")
    relation_digest = hashlib.sha256(
        "\0".join(sorted(guarded_relations)).encode("utf-8")
    ).hexdigest()[:24]
    permission_digest = hashlib.sha256(permission.encode("utf-8")).hexdigest()[:24]
    result_ref = result_id if result_id is not None else "aggregate"
    source_refs = (
        f"database-source:{source_id}:snapshot:{snapshot_id}:relations:{relation_digest}",
        f"database-result:{query_id}:{result_ref}",
    )
    queried_at = getattr(answer, "queried_at", None)
    if isinstance(queried_at, datetime):
        queried_at_text = queried_at.isoformat()
    elif isinstance(queried_at, str):
        queried_at_text = queried_at
    else:
        raise _error("CHART_DATABASE_TIME_INVALID", "answer queried_at is required")
    scope_label = {
        "current_page": f"当前页（{len(rows)} 行）",
        "all_aggregated": f"全部匹配数据（服务端聚合，{len(rows)} 项）",
        "top_n": f"前 {top_n} 项（服务端聚合）",
    }[scope]
    spec = ChartSpec(
        chart_type=chart_type,
        title=title,
        data_ref=f"database-query:{query_id}",
        dimension_column=dimension_column,
        metric_column=metric_column,
        statistic=statistic,
        scope=scope,
        scope_label=scope_label,
        source_refs=source_refs,
        queried_at=queried_at_text,
        revision=f"schema:{snapshot_id}:source:{source_version}:permission:{permission_digest}",
        unit=unit,
        sort_order=cast(SortOrder, sort_order),
        dimension_kind=cast(DimensionKind, dimension_kind),
        aggregate_complete=complete,
        top_n=top_n,
        display_precision=display_precision,
    )
    return ChartSnapshot.freeze(spec, rows)


def _chart_points(result: Any, dimension_column: str, metric_column: str) -> Iterator[ChartPoint]:
    rows = getattr(result, "rows", None)
    if not isinstance(rows, (tuple, list)):
        raise _error("CHART_DATABASE_ROWS_INVALID", "result rows must be bounded")
    for row in rows:
        if not isinstance(row, Mapping):
            raise _error("CHART_DATABASE_ROWS_INVALID", "result rows must be mappings")
        if dimension_column not in row or metric_column not in row:
            raise _error("CHART_DATABASE_COLUMN_INVALID", "chart columns are absent from result")
        dimension = row[dimension_column]
        if dimension is None or isinstance(dimension, bool):
            raise _error("CHART_DIMENSION_INVALID", "chart dimensions must be present")
        if isinstance(dimension, (datetime, date)):
            dimension_text = dimension.isoformat()
        elif isinstance(dimension, (str, int, Decimal)):
            dimension_text = str(dimension)
        else:
            raise _error("CHART_DIMENSION_INVALID", "chart dimensions must be scalar")
        yield ChartPoint(dimension_text, row[metric_column])


def _verify_query_context(
    answer: Any,
    query_context: Any,
    *,
    query_id: str,
    owner_user_id: int,
    conversation_id: int,
) -> None:
    if (
        query_context is None
        or getattr(query_context, "query_id", None) != query_id
        or getattr(query_context, "owner_user_id", None) != owner_user_id
        or getattr(query_context, "conversation_id", None) != conversation_id
    ):
        raise _error("CHART_QUERY_CONTEXT_INVALID", "query context does not match request")
    context_prepared = getattr(query_context, "prepared", None)
    context_binding = getattr(context_prepared, "binding", None)
    answer_binding = getattr(getattr(answer, "prepared", None), "binding", None)
    if context_binding is None or answer_binding is None:
        raise _error("CHART_QUERY_CONTEXT_INVALID", "query context has no binding")
    for field in (
        "database_source_id",
        "schema_snapshot_id",
        "source_version",
        "permission_fingerprint",
    ):
        if getattr(context_binding, field, None) != getattr(answer_binding, field, None):
            raise _error("CHART_QUERY_CONTEXT_INVALID", "query context binding changed")
    context_guarded = getattr(query_context, "guarded_query", None)
    answer_guarded = getattr(answer, "guarded_query", None)
    if (
        context_guarded is None
        or answer_guarded is None
        or tuple(getattr(context_guarded, "referenced_relations", ()))
        != tuple(getattr(answer_guarded, "referenced_relations", ()))
    ):
        raise _error("CHART_QUERY_CONTEXT_INVALID", "query context query changed")


def _verify_current_page(
    answer: Any,
    query_context: Any,
    page: Any,
    *,
    query_id: str,
    result_id: str,
    owner_user_id: int,
    conversation_id: int,
) -> None:
    if page is None:
        raise _error("CHART_RESULT_PAGE_INVALID", "current_page requires request-local page")
    if (
        getattr(page, "query_id", None) != query_id
        or getattr(page, "result_id", None) != result_id
        or getattr(page, "owner_user_id", None) != owner_user_id
        or getattr(page, "conversation_id", None) != conversation_id
        or getattr(query_context, "current_result_id", None) != result_id
        or getattr(query_context, "current_page_index", None)
        != getattr(getattr(answer, "prepared", None), "page_index", None)
        or getattr(page, "page_index", None)
        != getattr(getattr(answer, "prepared", None), "page_index", None)
        or getattr(page, "has_more", None)
        != getattr(getattr(answer, "result", None), "has_more", None)
    ):
        raise _error("CHART_RESULT_PAGE_INVALID", "current page does not match query context")
    answer_binding = getattr(getattr(answer, "prepared", None), "binding", None)
    for field in ("schema_snapshot_id", "source_version", "permission_fingerprint"):
        if getattr(page, field, None) != getattr(answer_binding, field, None):
            raise _error("CHART_RESULT_PAGE_INVALID", "current page binding changed")
    answer_rows = tuple(dict(row) for row in getattr(getattr(answer, "result", None), "rows", ()))
    page_rows = tuple(dict(row) for row in getattr(page, "rows", ()))
    if answer_rows != page_rows:
        raise _error("CHART_RESULT_PAGE_INVALID", "current page rows changed")


def _safe_id(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip():
        raise _error("CHART_DATABASE_BINDING_INVALID", f"{field} is required")
    return str(value)


def _error(code: str, message: str) -> ValueError:
    from .models import ChartValidationError

    return ChartValidationError(code, message)


__all__ = ["freeze_database_answer"]
