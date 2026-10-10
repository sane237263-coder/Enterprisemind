"""Request-local database gateway. Only control fields have a durable projection."""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_mind.agent.diagnostics import record_failure
from enterprise_mind.agent.schemas import DatabaseQueryInput
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.database.contracts import (
    GeneratedQuery,
    GuardedQuery,
    QueryResult,
    SchemaCatalog,
)
from enterprise_mind.database.projection import (
    BoundSchema,
    authorized_schema,
    catalog_projection,
    detail_projection,
    revalidate_schema,
)
from enterprise_mind.database.sql_provider import (
    DatabaseModel,
    DatabaseSelection,
    SQLDraft,
)
from enterprise_mind.identity.service import CurrentIdentity


class QueryTransport(Protocol):
    async def execute_query(
        self, query: GuardedQuery, catalog: SchemaCatalog, snapshot_id: int
    ) -> QueryResult: ...


class QueryAmbiguity(Exception):
    def __init__(self, kind: str, binding: BoundSchema, relations: list[tuple[str, str]]) -> None:
        self.kind, self.binding, self.relations = kind, binding, relations
        super().__init__("DATABASE_QUERY_AMBIGUOUS")


MAX_CATALOG_RELATIONS_PER_BATCH = 32
MAX_CATALOG_BATCH_BYTES = 16 * 1024
MAX_CATALOG_BATCHES = 64
_RETRIABLE_GUARD_REASONS = frozenset(
    {
        "relation is not authorized",
        "unqualified relation",
        "field is not authorized",
        "stable ordering is required",
        "wildcard is not allowed",
        "unsupported expression",
        "parameter count mismatch",
        "invalid text",
    }
)


_BOUNDED_RESULT_RE = re.compile(
    r"(?ix)(?:\btop\s*\d+\b|\blimit\s+\d+\b|"
    r"(?:前|最多|至多)\s*\d+\s*(?:条|项|个|行|记录|结果|订单)?)"
)
_PAGE_SIZE_RE = re.compile(
    r"(?ix)每页\s*(?:返回|展示|显示)?\s*(?:最多\s*)?\d+\s*"
    r"(?:条|项|个|行|记录|结果|订单)"
)
_DETAIL_REQUEST_RE = re.compile(r"明细|逐条|逐行")
_AGGREGATE_REQUEST_RE = re.compile(r"统计|汇总|总数|总额|数量|平均|占比")


def is_explicit_detail_request(question: str) -> bool:
    return bool(_DETAIL_REQUEST_RE.search(question)) and not bool(
        _AGGREGATE_REQUEST_RE.search(question)
    )


def is_bounded_result_request(question: str) -> bool:
    """Return whether the user explicitly requested a business result cap.

    Transport page size is deliberately excluded: ``每页返回 100 行`` must
    still execute the full query and let the server own LIMIT/OFFSET.  This
    check only protects against an LLM adding a business LIMIT to an otherwise
    unbounded request; explicit first-N/Top-N queries keep their SQL limit.
    """

    # Page-size wording is transport control, even when it contains “最多”.
    # Remove it before looking for a business first-N request.
    normalized = _PAGE_SIZE_RE.sub(" ", question or "")
    return bool(_BOUNDED_RESULT_RE.search(normalized))


def _catalog_prompt_budget(model: DatabaseModel) -> int:
    provider = getattr(model, "model", None)
    maximum = getattr(provider, "max_input_tokens", None)
    output = getattr(provider, "max_output_tokens", None)
    if not isinstance(maximum, int) or not isinstance(output, int):
        return MAX_CATALOG_BATCH_BYTES
    # Leave room for the task instruction, user question, semantics and
    # structured-output schema.  This is a preflight bound; the provider still
    # performs its authoritative token check.
    available_tokens = max(512, maximum - output - 2048)
    return min(MAX_CATALOG_BATCH_BYTES, max(4096, available_tokens * 2))


def _catalog_batches(
    catalog: list[dict[str, Any]], *, max_bytes: int = MAX_CATALOG_BATCH_BYTES
) -> list[list[dict[str, Any]]]:
    """Keep relation-selection prompts bounded without dropping authorized objects."""

    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for relation in catalog:
        candidate = [*current, relation]
        encoded = len(
            json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if current and (len(candidate) > MAX_CATALOG_RELATIONS_PER_BATCH or encoded > max_bytes):
            batches.append(current)
            current = [relation]
        else:
            current = candidate
    if current:
        batches.append(current)
    if len(batches) > MAX_CATALOG_BATCHES:
        raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
    return batches


@dataclass(repr=False)
class PreparedQuery:
    binding: BoundSchema
    catalog: SchemaCatalog
    question: str
    semantics: list[str]
    page_size: int
    page_index: int

    def controls(self) -> dict[str, Any]:
        return {
            "database_source_id": str(self.binding.database_source_id),
            "schema_snapshot_id": str(self.binding.schema_snapshot_id),
            "page_size": self.page_size,
            "page_index": self.page_index,
        }


@dataclass(repr=False)
class DatabaseAnswer:
    prepared: PreparedQuery
    result: QueryResult
    queried_at: datetime
    conditions_summary: str
    query_kind: str = "detail"
    guarded_query: GuardedQuery | None = field(default=None, repr=False)
    continuation_available: bool = True

    def summary(self) -> dict[str, Any]:
        return {"ok": True, "row_count": self.result.row_count, "has_more": self.result.has_more}

    def locator(self) -> dict[str, Any]:
        return {
            "database_source_name": self.prepared.binding.source_name,
            "database_name": self.prepared.binding.database_name,
            "page_size": self.prepared.page_size,
            "page_index": self.prepared.page_index,
            "query_kind": self.query_kind,
            "returned_rows": self.result.row_count,
            "continuation_available": self.continuation_available,
        }


def explicit_chart_query(question: str) -> tuple[str, str, str, str] | None:
    """Recognize a literal table and SUM/grouping request, without generating SQL."""

    if not re.search(r"柱状图|折线图|饼图", question) or re.search(
        r"知识库|文档|文件|分析|解释|原因|建议|总结|对比|比较|最近|近期", question
    ):
        return None
    relations = set(re.findall(r"(?<![\w.])([A-Za-z_]\w*)\.([A-Za-z_]\w*)(?![\w.])", question))
    aggregation = re.search(
        r"按\s+([A-Za-z_]\w*)\s+分组汇总\s+SUM\(([A-Za-z_]\w*)\)", question, re.I
    )
    if len(relations) != 1 or aggregation is None:
        return None
    schema, relation = next(iter(relations))
    return schema, relation, aggregation.group(1), aggregation.group(2)


class DatabaseQueryService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        model: DatabaseModel,
        transport: QueryTransport,
    ) -> None:
        self.sessions, self.model, self.transport = sessions, model, transport

    async def available(self, identity: CurrentIdentity) -> bool:
        try:
            async with self.sessions() as session:
                binding = await authorized_schema(session, identity)
            return bool(binding.catalog.relations)
        except APIError as error:
            if error.code in {
                ErrorCode.DATABASE_SOURCE_UNAVAILABLE,
                ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE,
                ErrorCode.DATABASE_RELATION_FORBIDDEN,
            }:
                return False
            raise

    async def prepare(
        self, identity: CurrentIdentity, body: DatabaseQueryInput, semantics: list[str]
    ) -> PreparedQuery:
        async with self.sessions() as session:
            binding = await authorized_schema(session, identity)
        projected_catalog = catalog_projection(binding)
        explicit = explicit_chart_query(body.question)
        if explicit is not None:
            schema_name, relation_name, dimension, metric = explicit
            explicit_relation = next(
                (
                    item
                    for item in binding.catalog.relations
                    if item.key == (schema_name, relation_name)
                ),
                None,
            )
            if explicit_relation is None:
                raise APIError(ErrorCode.DATABASE_RELATION_FORBIDDEN)
            if not {dimension, metric} <= {column.name for column in explicit_relation.columns}:
                raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
            # Bind only to the freshly authorized snapshot. The model still
            # generates SQL, and application/MCP Guards validate it as usual.
            return PreparedQuery(
                binding,
                SchemaCatalog((explicit_relation,)),
                body.question,
                semantics,
                body.page_size,
                body.page_index,
            )
        selections: list[Any] = []
        selected_keys: set[tuple[str, str]] = set()
        clarification: Literal["none", "relation", "time_range", "metric", "status"] = "none"
        for batch in _catalog_batches(
            projected_catalog, max_bytes=_catalog_prompt_budget(self.model)
        ):
            selection = await self.model.select_relations(body.question, semantics, batch)
            batch_keys = {(item["schema_name"], item["relation_name"]) for item in batch}
            for relation in selection.relations:
                key = (relation.schema_name, relation.relation_name)
                if key not in batch_keys:
                    raise APIError(ErrorCode.DATABASE_RELATION_FORBIDDEN)
                if key in selected_keys:
                    continue
                selected_keys.add(key)
                selections.append(relation)
                if len(selections) > 50:
                    raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
            if clarification == "none" and selection.clarification != "none":
                clarification = selection.clarification
        selection = DatabaseSelection(
            relations=selections,
            clarification=clarification,
        )
        keys = [(item.schema_name, item.relation_name) for item in selection.relations]
        if any(key not in binding.catalog for key in keys):
            raise APIError(ErrorCode.DATABASE_RELATION_FORBIDDEN)
        if selection.clarification != "none":
            raise QueryAmbiguity(selection.clarification, binding, keys)
        if not keys:
            raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
        catalog = SchemaCatalog(tuple(r for r in binding.catalog.relations if r.key in keys))
        return PreparedQuery(
            binding, catalog, body.question, semantics, body.page_size, body.page_index
        )

    async def execute(self, identity: CurrentIdentity, prepared: PreparedQuery) -> DatabaseAnswer:
        from enterprise_mind.database.guard import (
            SQLGuard,
            SQLGuardError,
            infer_query_kind,
            summarize_conditions,
        )

        generated_at = datetime.now(UTC)
        schema = detail_projection(prepared.binding, [r.key for r in prepared.catalog.relations])
        draft = await self.model.generate_sql(
            prepared.question,
            prepared.semantics,
            schema,
            current_time_utc=generated_at,
        )
        guard = SQLGuard(prepared.catalog)
        exact_relation_named = any(
            re.search(
                rf"(?<![\w.]){re.escape(relation.schema_name + '.' + relation.relation_name)}"
                r"(?![\w.])",
                prepared.question,
            )
            for relation in prepared.catalog.relations
        )

        def validate(draft: SQLDraft) -> GuardedQuery:
            # The provider's conditions_summary is deliberately never a persistence input.
            generated = GeneratedQuery(draft.sql, tuple(draft.parameters), draft.query_kind)
            try:
                return guard.validate(
                    generated, page_size=prepared.page_size, page_index=prepared.page_index
                )
            except SQLGuardError as error:
                record_failure(
                    "database.sql_guard_corrected." + error.reason.replace(" ", "_"), error
                )
                code = (
                    ErrorCode.DATABASE_RELATION_FORBIDDEN
                    if error.code == "DATABASE_RELATION_FORBIDDEN"
                    else ErrorCode.DATABASE_QUERY_REJECTED
                )
                raise APIError(code) from None

        corrected_once = False
        try:
            query = guard.validate(
                GeneratedQuery(draft.sql, tuple(draft.parameters), draft.query_kind),
                page_size=prepared.page_size,
                page_index=prepared.page_index,
            )
        except SQLGuardError as error:
            # Guard reasons are a fixed allowlist; never emit SQL, parameters or model output.
            record_failure("database.sql_guard." + error.reason.replace(" ", "_"), error)
            if not exact_relation_named or error.reason not in _RETRIABLE_GUARD_REASONS:
                code = (
                    ErrorCode.DATABASE_RELATION_FORBIDDEN
                    if error.code == "DATABASE_RELATION_FORBIDDEN"
                    else ErrorCode.DATABASE_QUERY_REJECTED
                )
                raise APIError(code) from None
            # One correction is allowed only for a question explicitly naming
            # a selected authorized relation. The next SQL must pass the same Guard.
            draft = await self.model.generate_sql(
                prepared.question,
                prepared.semantics,
                schema,
                retry_guard_reason=error.reason,
                current_time_utc=generated_at,
            )
            query = validate(draft)
            corrected_once = True
        if infer_query_kind(query.validated_query) == "aggregate" and is_explicit_detail_request(
            prepared.question
        ):
            if corrected_once:
                raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
            draft = await self.model.generate_sql(
                prepared.question,
                prepared.semantics,
                schema,
                retry_detail_required=True,
                current_time_utc=generated_at,
            )
            query = validate(draft)
            if infer_query_kind(query.validated_query) == "aggregate":
                raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
        if query.has_business_limit and not is_bounded_result_request(prepared.question):
            corrected = guard.without_top_level_limit(query)
            if corrected is None:
                # A nested or otherwise non-removable cap needs one constrained
                # model correction. The user request, schema and clock stay fixed.
                draft = await self.model.generate_sql(
                    prepared.question,
                    prepared.semantics,
                    schema,
                    retry_without_business_limit=True,
                    current_time_utc=generated_at,
                )
                query = validate(draft)
                if query.has_business_limit:
                    corrected = guard.without_top_level_limit(query)
                    if corrected is None:
                        raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
            query = corrected or query
        if not query.referenced_relations:
            raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
        actual_kind = infer_query_kind(query.validated_query)
        kind = "聚合统计" if actual_kind == "aggregate" else "明细查询"
        summary = (
            f"{kind}；{summarize_conditions(query.validated_query)}；"
            f"第 {prepared.page_index + 1} 页，"
            f"每页最多 {prepared.page_size} 行。"
        )
        try:
            return await self._execute_guarded(
                identity,
                prepared,
                query,
                query_kind=actual_kind,
                conditions_summary=summary,
                continuation_available=guard.continuation_available(query),
            )
        except APIError as error:
            if (
                error.code != ErrorCode.DATABASE_QUERY_REJECTED
                or not exact_relation_named
                or corrected_once
            ):
                raise
            # A Guard-valid statement can still be rejected by PostgreSQL.
            # Give an explicitly named authorized relation one fresh SQL draft.
            draft = await self.model.generate_sql(
                prepared.question,
                prepared.semantics,
                schema,
                retry_execution_rejected=True,
                current_time_utc=generated_at,
            )
            query = validate(draft)
            if not query.referenced_relations:
                raise APIError(ErrorCode.DATABASE_QUERY_REJECTED) from None
            if query.has_business_limit and not is_bounded_result_request(prepared.question):
                corrected = guard.without_top_level_limit(query)
                if corrected is None:
                    raise APIError(ErrorCode.DATABASE_QUERY_REJECTED) from None
                query = corrected
            actual_kind = infer_query_kind(query.validated_query)
            if actual_kind == "aggregate" and is_explicit_detail_request(prepared.question):
                raise APIError(ErrorCode.DATABASE_QUERY_REJECTED) from None
            kind = "聚合统计" if actual_kind == "aggregate" else "明细查询"
            summary = (
                f"{kind}；{summarize_conditions(query.validated_query)}；"
                f"第 {prepared.page_index + 1} 页，"
                f"每页最多 {prepared.page_size} 行。"
            )
            return await self._execute_guarded(
                identity,
                prepared,
                query,
                query_kind=actual_kind,
                conditions_summary=summary,
                continuation_available=guard.continuation_available(query),
            )

    async def execute_page(
        self,
        identity: CurrentIdentity,
        prepared: PreparedQuery,
        guarded: GuardedQuery,
        *,
        query_kind: str,
        conditions_summary: str,
    ) -> DatabaseAnswer:
        """Execute a stored guarded definition at exactly one next page."""

        from enterprise_mind.database.guard import (
            SQLGuard,
            SQLGuardError,
            infer_query_kind,
            summarize_conditions,
        )

        if guarded.validated_query == "":
            raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
        guard = SQLGuard(prepared.catalog)
        try:
            query = guard.validate(
                GeneratedQuery(
                    guarded.validated_query,
                    guarded.validated_parameters,
                    query_kind=query_kind,
                ),
                page_size=prepared.page_size,
                page_index=prepared.page_index,
            )
        except SQLGuardError as error:
            code = (
                ErrorCode.DATABASE_RELATION_FORBIDDEN
                if error.code == "DATABASE_RELATION_FORBIDDEN"
                else ErrorCode.DATABASE_QUERY_REJECTED
            )
            raise APIError(code) from None
        if query.referenced_relations != guarded.referenced_relations:
            raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
        async with self.sessions() as session:
            current = await authorized_schema(session, identity)
        if (
            current.schema_snapshot_id != prepared.binding.schema_snapshot_id
            or current.source_version != prepared.binding.source_version
            or current.permission_fingerprint != prepared.binding.permission_fingerprint
        ):
            raise APIError(ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE)
        actual_kind = infer_query_kind(query.validated_query)
        kind = "聚合统计" if actual_kind == "aggregate" else "明细查询"
        page_summary = (
            f"{kind}；{summarize_conditions(query.validated_query)}；"
            f"第 {prepared.page_index + 1} 页，"
            f"每页最多 {prepared.page_size} 行。"
        )
        return await self._execute_guarded(
            identity,
            prepared,
            query,
            query_kind=actual_kind,
            conditions_summary=page_summary,
            continuation_available=guard.continuation_available(query),
        )

    async def _execute_guarded(
        self,
        identity: CurrentIdentity,
        prepared: PreparedQuery,
        query: GuardedQuery,
        *,
        query_kind: str,
        conditions_summary: str,
        continuation_available: bool,
    ) -> DatabaseAnswer:
        async with self.sessions() as session:
            current = await authorized_schema(
                session, identity, snapshot_id=prepared.binding.schema_snapshot_id
            )
            await revalidate_schema(session, identity, prepared.binding, query.referenced_relations)
            if current.permission_fingerprint != prepared.binding.permission_fingerprint:
                raise APIError(ErrorCode.DATABASE_RELATION_FORBIDDEN)
        result = await self.transport.execute_query(
            query, prepared.catalog, prepared.binding.schema_snapshot_id
        )
        if result.referenced_relations != query.referenced_relations:
            raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
        async with self.sessions() as session:
            current = await authorized_schema(
                session, identity, snapshot_id=prepared.binding.schema_snapshot_id
            )
            await revalidate_schema(session, identity, prepared.binding, query.referenced_relations)
            if current.permission_fingerprint != prepared.binding.permission_fingerprint:
                raise APIError(ErrorCode.DATABASE_RELATION_FORBIDDEN)
        return DatabaseAnswer(
            prepared,
            result,
            datetime.now(UTC),
            conditions_summary,
            query_kind=query_kind,
            guarded_query=query,
            continuation_available=continuation_available,
        )

    async def verify(self, identity: CurrentIdentity, answer: DatabaseAnswer) -> None:
        async with self.sessions() as session:
            await revalidate_schema(
                session, identity, answer.prepared.binding, answer.result.referenced_relations
            )
