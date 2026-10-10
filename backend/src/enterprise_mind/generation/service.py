"""Durable content drafts and deterministic render task submission."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.agent.generation_models import ContentDraft, GeneratedFile, GenerationTask
from enterprise_mind.agent.service import conversation
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.charts.models import MAX_INPUT_ROWS
from enterprise_mind.database.catalog import catalog_to_wire
from enterprise_mind.database.export_models import DatabaseExportPlan
from enterprise_mind.database.export_runtime import (
    EXPORT_PLAN_TTL_SECONDS,
    ExportRuntimePlan,
    ExportRuntimePlanStore,
)
from enterprise_mind.database.pages import DatabasePage, DatabasePageStore
from enterprise_mind.database.projection import authorized_schema, revalidate_schema
from enterprise_mind.database.query_context import DatabaseQueryContextStore
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.models import Document, OutboxEvent, Revision
from enterprise_mind.knowledge.models import KnowledgeBase

SUPPORTED_FORMATS = {"docx", "pdf", "md", "xlsx"}
CHART_FORMATS = {"png", "pdf", "xlsx", "csv"}
CHART_TYPES = {"bar", "line", "pie"}
CHART_SCOPES = {"current_page", "all_aggregated", "top_n"}
_CHART_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_DRAFT_CHARS = 10_000_000


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def draft_hash(
    title: str, body_markdown: str, tables: list[dict[str, Any]], source_refs: list[dict[str, Any]]
) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "title": title,
                "body_markdown": body_markdown,
                "tables": tables,
                "source_refs": source_refs,
            }
        )
    ).hexdigest()


def _validate_draft(
    title: str,
    body_markdown: str,
    tables: list[dict[str, Any]],
    source_refs: list[dict[str, Any]],
) -> None:
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 300:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    if not isinstance(body_markdown, str) or not body_markdown.strip():
        raise APIError(ErrorCode.VALIDATION_ERROR)
    if len(body_markdown) > MAX_DRAFT_CHARS:
        raise APIError(ErrorCode.FILE_TOO_LARGE)
    if (
        not isinstance(tables, list)
        or len(tables) > 32
        or not all(isinstance(item, dict) for item in tables)
    ):
        raise APIError(ErrorCode.VALIDATION_ERROR)
    if (
        not isinstance(source_refs, list)
        or len(source_refs) > 100
        or not all(isinstance(item, dict) for item in source_refs)
    ):
        raise APIError(ErrorCode.VALIDATION_ERROR)


def draft_output(item: ContentDraft) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "conversation_id": str(item.conversation_id) if item.conversation_id else None,
        "title": item.title,
        "body_markdown": item.body_markdown,
        "tables": item.tables or [],
        "source_refs": item.source_refs or [],
        "revision": item.revision,
        "content_hash": item.content_hash,
        "status": item.status,
        "created_at": item.created_at.astimezone(UTC).isoformat() if item.created_at else None,
        "updated_at": item.updated_at.astimezone(UTC).isoformat() if item.updated_at else None,
    }


def task_output(
    task: GenerationTask,
    file: GeneratedFile | None = None,
    *,
    files: list[GeneratedFile] | None = None,
) -> dict[str, Any]:
    all_files = list(files or ())
    if file is not None and all(item.id != file.id for item in all_files):
        all_files.append(file)
    selected = next((item for item in all_files if item.file_format == task.format), None)
    selected = selected or file or (all_files[0] if all_files else None)
    complete = task.status == "succeeded" and selected is not None
    result: dict[str, Any] = {
        "task_id": str(task.id),
        "kind": task.kind,
        "status": task.status,
        "phase": task.phase,
        "format": task.format,
        "scope": task.scope,
        "processed_rows": task.processed_rows,
        "total_rows": task.total_rows,
        "exported_rows": task.exported_rows,
        "file_id": str(selected.id)
        if selected is not None and task.status == "succeeded"
        else None,
        "complete": complete,
        "error": {"code": task.error_code, "message": task.error_message}
        if task.error_code
        else None,
    }
    if not complete or selected is None:
        return result
    result["file_name"] = selected.file_name
    result["size_bytes"] = selected.size_bytes
    result["source_refs"] = selected.source_refs or []
    result["generated_at"] = (
        selected.generated_at.astimezone(UTC).isoformat() if selected.generated_at else None
    )
    if task.kind == "chart_render":
        chart = task.input_ref.get("chart") if isinstance(task.input_ref, dict) else None
        if isinstance(chart, dict):
            result["chart"] = {
                key: chart[key]
                for key in (
                    "chart_type",
                    "title",
                    "scope",
                    "dimension_column",
                    "metric_column",
                    "statistic",
                    "dimension_kind",
                    "sort_order",
                    "top_n",
                )
                if key in chart
            }
        chart_files = sorted(all_files, key=lambda item: item.file_format)
        result["artifacts"] = [
            {
                "file_id": str(item.id),
                "format": item.file_format,
                "file_name": item.file_name,
                "size_bytes": item.size_bytes,
                "download_url": f"/api/v1/generated-files/{item.id}/download",
                **(
                    {"preview_url": f"/api/v1/generated-files/{item.id}/preview"}
                    if item.file_format == "png"
                    else {}
                ),
            }
            for item in chart_files
        ]
        png = next((item for item in chart_files if item.file_format == "png"), None)
        result["preview_url"] = (
            f"/api/v1/generated-files/{png.id}/preview" if png is not None else None
        )
        result["download_url"] = f"/api/v1/generated-files/{selected.id}/download"
    return result


def _positive_id(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return int(value)
    return None


def _derived_chart_statistic(context: Any, dimension: str, metric: str) -> str:
    """Describe the server guarded query, never a model supplied claim."""

    if context.query_kind != "aggregate":
        return f"当前页查询结果（未聚合，维度 {dimension}，指标 {metric}）"
    sql = str(getattr(context.guarded_query, "validated_query", ""))

    # ``metric`` is normally the result-column alias (for example
    # ``total_amount``), not the input column inside the aggregate.  Parsing
    # the already Guard-validated SELECT target list lets us describe the
    # selected metric without guessing from the first aggregate in the query.
    # If the target is an expression we cannot safely render, use a generic
    # description instead of inventing a function/column pair.
    function: str | None = None
    argument: str | None = None
    matches: list[tuple[str, Any, tuple[str, ...], tuple[str, ...]]] = []
    try:
        from pglast import parse_sql

        statements = tuple(parse_sql(sql))
        statement = getattr(statements[0], "stmt", None) if len(statements) == 1 else None
        targets = tuple(getattr(statement, "targetList", ()) or ())
        for target in targets:
            alias = getattr(target, "name", None)
            value = getattr(target, "val", None)
            value_type = type(value).__name__
            names = tuple(
                str(item.sval)
                for item in (getattr(value, "fields", ()) or ())
                if getattr(item, "sval", None) is not None
            )
            func_names = tuple(
                str(item.sval).lower()
                for item in (getattr(value, "funcname", ()) or ())
                if getattr(item, "sval", None) is not None
            )
            if alias == metric or (not alias and names and names[-1] == metric):
                matches.append((value_type, value, func_names, names))
        if len(matches) == 1:
            value_type, value, func_names, _ = matches[0]
            if (
                value_type == "FuncCall"
                and func_names
                and func_names[-1]
                in {
                    "sum",
                    "avg",
                    "count",
                    "min",
                    "max",
                }
            ):
                function = func_names[-1].upper()
                if getattr(value, "agg_star", False):
                    argument = "*"
                else:
                    args = tuple(getattr(value, "args", ()) or ())
                    if len(args) == 1 and type(args[0]).__name__ == "ColumnRef":
                        fields = tuple(
                            str(item.sval)
                            for item in (getattr(args[0], "fields", ()) or ())
                            if getattr(item, "sval", None) is not None
                        )
                        if 1 <= len(fields) <= 3 and re.fullmatch(
                            r"[A-Za-z_][A-Za-z0-9_]*", fields[-1]
                        ):
                            # The guarded aggregate may qualify its input with a
                            # table alias or schema. Preserve its actual field.
                            argument = fields[-1]
    except (ImportError, IndexError, TypeError, ValueError):
        # The Guard has already accepted the query; inability to inspect its
        # AST only limits wording precision and must not create a false claim.
        pass

    grouped = bool(re.search(r"\bGROUP\s+BY\b", sql, re.IGNORECASE))
    time_grouped = bool(re.search(r"date_trunc|date_bin|AT\s+TIME\s+ZONE", sql, re.IGNORECASE))
    suffix = f"；按 {dimension} 分组" if grouped else ""
    if time_grouped:
        suffix += "；包含服务端时间分组"
    if function is None or argument is None:
        return f"服务端聚合结果（指标 {metric}）{suffix}"
    distinct = (
        "DISTINCT " if len(matches) == 1 and getattr(matches[0][1], "agg_distinct", False) else ""
    )
    return f"{function}({distinct}{argument}){suffix}"


def _validate_chart_point_budget(chart_type: str, chart_scope: str, top_n: int | None) -> None:
    """Reject a chart Top N before its export plan or outbox row is created."""

    if chart_scope == "top_n" and top_n is not None and top_n > MAX_INPUT_ROWS[chart_type]:
        raise APIError(ErrorCode.QUERY_RESULT_TOO_LARGE)


async def _validated_generated_source_refs(
    session: AsyncSession,
    identity: CurrentIdentity,
    source_refs: list[dict[str, Any]],
    *,
    current_access_only: bool,
    bind_current_permission: bool = False,
) -> list[dict[str, Any]]:
    validated_refs: list[dict[str, Any]] = []
    for source in source_refs:
        if not isinstance(source, dict):
            raise APIError(ErrorCode.PERMISSION_DENIED)
        validated_source = dict(source)
        source_type = source.get("source_type")
        if source_type == "knowledge":
            document_id = _positive_id(source.get("document_id"))
            revision_id = _positive_id(source.get("revision_id"))
            if document_id is None or revision_id is None:
                raise APIError(ErrorCode.PERMISSION_DENIED)
            visible = await session.scalar(
                select(Document.id)
                .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
                .join(Revision, Revision.document_id == Document.id)
                .where(
                    Document.id == document_id,
                    Document.active_revision_id == revision_id,
                    Document.deleted_at.is_(None),
                    Revision.id == revision_id,
                    Revision.retired_at.is_(None),
                    Revision.processing_status.in_(("success", "partial_success")),
                    AuthorizationService.knowledge_scope(identity),
                )
            )
            if visible is None:
                raise APIError(ErrorCode.PERMISSION_DENIED)
        elif source_type == "database":
            source_id = _positive_id(source.get("database_source_id"))
            snapshot_id = _positive_id(source.get("schema_snapshot_id"))
            relations = source.get("referenced_relations")
            if (
                source_id is None
                or snapshot_id is None
                or not isinstance(relations, list)
                or not relations
            ):
                raise APIError(ErrorCode.PERMISSION_DENIED)
            binding = await authorized_schema(session, identity, snapshot_id=snapshot_id)
            allowed = {relation.quoted_qualified_name for relation in binding.catalog.relations}
            if binding.database_source_id != source_id or any(
                not isinstance(relation, str) or relation not in allowed for relation in relations
            ):
                raise APIError(ErrorCode.PERMISSION_DENIED)
            expected_source_version = source.get("source_version")
            expected_permission = source.get("permission_fingerprint")
            if (
                expected_source_version is not None
                and expected_source_version != binding.source_version
            ) or (
                not current_access_only
                and expected_permission is not None
                and expected_permission != binding.permission_fingerprint
            ):
                raise APIError(ErrorCode.PERMISSION_DENIED)
            if bind_current_permission:
                validated_source["permission_fingerprint"] = binding.permission_fingerprint
        else:
            raise APIError(ErrorCode.PERMISSION_DENIED)
        validated_refs.append(validated_source)
    return validated_refs


async def revalidate_generated_sources(
    session: AsyncSession,
    identity: CurrentIdentity,
    source_refs: list[dict[str, Any]],
    *,
    current_access_only: bool = False,
) -> None:
    """Require current access to every restricted source behind a generated file.

    Query plans and workers retain their permission fingerprint as a binding
    to the authorization state that created them. Persisted result reads may
    instead check current access, so a password reset does not make an
    otherwise authorized result unreadable. A result becomes readable again
    if its current source grant is restored.
    """

    await _validated_generated_source_refs(
        session,
        identity,
        source_refs,
        current_access_only=current_access_only,
    )


async def rebind_generated_sources(
    session: AsyncSession,
    identity: CurrentIdentity,
    source_refs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate current source access and bind task-local refs to that state."""

    return await _validated_generated_source_refs(
        session,
        identity,
        source_refs,
        current_access_only=True,
        bind_current_permission=True,
    )


async def get_draft(
    session: AsyncSession,
    identity: CurrentIdentity,
    draft_id: int,
    *,
    lock: bool = False,
    current_access_only: bool = False,
) -> ContentDraft:
    query = select(ContentDraft).where(
        ContentDraft.id == draft_id,
        ContentDraft.owner_user_id == identity.user.id,
    )
    item = await session.scalar(query.with_for_update() if lock else query)
    if item is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    await revalidate_generated_sources(
        session,
        identity,
        list(item.source_refs or []),
        current_access_only=current_access_only,
    )
    return item


async def create_draft(
    session: AsyncSession,
    identity: CurrentIdentity,
    *,
    title: str,
    body_markdown: str,
    tables: list[dict[str, Any]] | None = None,
    source_refs: list[dict[str, Any]] | None = None,
    conversation_id: int | None = None,
    source_message_id: int | None = None,
) -> ContentDraft:
    tables, source_refs = tables or [], source_refs or []
    title, body_markdown = title.strip(), body_markdown
    _validate_draft(title, body_markdown, tables, source_refs)
    if conversation_id is not None:
        await conversation(session, identity, conversation_id)
    item = ContentDraft(
        owner_user_id=identity.user.id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        title=title,
        body_markdown=body_markdown,
        tables=tables,
        source_refs=source_refs,
        content_hash=draft_hash(title, body_markdown, tables, source_refs),
        status="ready",
    )
    session.add(item)
    await session.flush()
    return item


async def update_draft(
    session: AsyncSession,
    identity: CurrentIdentity,
    draft_id: int,
    *,
    expected_revision: int,
    title: str | None = None,
    body_markdown: str | None = None,
    tables: list[dict[str, Any]] | None = None,
    source_refs: list[dict[str, Any]] | None = None,
) -> ContentDraft:
    item = await get_draft(session, identity, draft_id, lock=True, current_access_only=True)
    if item.revision != expected_revision:
        raise APIError(ErrorCode.ROW_VERSION_CONFLICT)
    next_title = item.title if title is None else title.strip()
    next_body = item.body_markdown if body_markdown is None else body_markdown
    next_tables = item.tables or [] if tables is None else tables
    refs = item.source_refs or [] if source_refs is None else source_refs
    _validate_draft(next_title, next_body, next_tables, refs)
    item.title, item.body_markdown, item.tables, item.source_refs = (
        next_title,
        next_body,
        next_tables,
        refs,
    )
    item.revision += 1
    item.content_hash = draft_hash(next_title, next_body, next_tables, refs)
    item.updated_at = datetime.now(UTC)
    await session.flush()
    return item


def _source_refs_without_permission_fingerprint(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list) or not all(isinstance(source, dict) for source in value):
        return None
    refs: list[dict[str, Any]] = []
    for source in value:
        normalized = dict(source)
        if normalized.get("source_type") == "database":
            normalized.pop("permission_fingerprint", None)
        refs.append(normalized)
    return refs


def _render_task_matches_request(
    task: GenerationTask,
    draft: ContentDraft,
    *,
    file_format: str,
    file_name: str | None,
) -> bool:
    frozen = task.input_ref if isinstance(task.input_ref, dict) else None
    if frozen is None:
        return False
    stored_refs = _source_refs_without_permission_fingerprint(frozen.get("source_refs"))
    draft_refs = _source_refs_without_permission_fingerprint(draft.source_refs or [])
    task_refs = _source_refs_without_permission_fingerprint(task.source_refs or [])
    if stored_refs is None or draft_refs is None or task_refs is None:
        return False
    frozen_tables = frozen.get("tables")
    frozen_refs = frozen.get("source_refs")
    if not isinstance(frozen_tables, list) or not isinstance(frozen_refs, list):
        return False
    stored_content_hash = frozen.get("content_hash")
    if not isinstance(stored_content_hash, str) or not hmac.compare_digest(
        stored_content_hash,
        draft_hash(
            str(frozen.get("title", "")),
            str(frozen.get("body_markdown", "")),
            frozen_tables,
            frozen_refs,
        ),
    ):
        return False
    try:
        stored_fingerprint = hashlib.sha256(_canonical(frozen)).hexdigest()
    except (TypeError, ValueError):
        return False
    return (
        task.kind == "content_render"
        and task.format == file_format
        and task.draft_id == draft.id
        and task.conversation_id == draft.conversation_id
        and task.source_message_id == draft.source_message_id
        and hmac.compare_digest(task.request_fingerprint, stored_fingerprint)
        and frozen.get("title") == draft.title
        and frozen.get("body_markdown") == draft.body_markdown
        and frozen.get("tables") == (draft.tables or [])
        and frozen.get("file_name") == file_name
        and frozen.get("draft_revision") == draft.revision
        and stored_refs == draft_refs == task_refs
    )


async def create_render_task(
    session: AsyncSession,
    identity: CurrentIdentity,
    *,
    draft_id: int,
    expected_revision: int,
    file_format: str,
    file_name: str | None,
    idempotency_key: str,
    ttl_seconds: int = 86400,
) -> GenerationTask:
    if file_format not in SUPPORTED_FORMATS or not idempotency_key:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    # A new render request is authorized against current source access and
    # receives its own current permission binding. The historical draft refs
    # remain unchanged; queued tasks and workers still use strict bindings.
    draft = await get_draft(session, identity, draft_id, lock=True, current_access_only=True)
    if draft.status != "ready":
        raise APIError(ErrorCode.VALIDATION_ERROR)
    if draft.revision != expected_revision:
        raise APIError(ErrorCode.ROW_VERSION_CONFLICT)
    existing = await session.scalar(
        select(GenerationTask).where(
            GenerationTask.owner_user_id == identity.user.id,
            GenerationTask.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if not _render_task_matches_request(
            existing,
            draft,
            file_format=file_format,
            file_name=file_name,
        ):
            raise APIError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
        return existing

    task_source_refs = await rebind_generated_sources(
        session, identity, list(draft.source_refs or [])
    )
    frozen = {
        "title": draft.title,
        "body_markdown": draft.body_markdown,
        "tables": draft.tables or [],
        "source_refs": task_source_refs,
        "file_name": file_name,
        "content_hash": draft_hash(
            draft.title,
            draft.body_markdown,
            draft.tables or [],
            task_source_refs,
        ),
        "draft_revision": draft.revision,
    }
    fingerprint = hashlib.sha256(_canonical(frozen)).hexdigest()
    task = GenerationTask(
        owner_user_id=identity.user.id,
        conversation_id=draft.conversation_id,
        kind="content_render",
        format=file_format,
        draft_id=draft.id,
        source_message_id=draft.source_message_id,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        input_ref=frozen,
        source_refs=task_source_refs,
        status="queued",
    )
    session.add(task)
    await session.flush()
    session.add(
        OutboxEvent(
            aggregate_type="generation_task",
            aggregate_id=task.id,
            event_type="generation.requested",
            payload={"task_id": str(task.id)},
            deduplication_key=f"generation:{task.id}",
        )
    )
    await session.flush()
    return task


def _page_matches_context(page: DatabasePage, query_id: str, result_id: str, context: Any) -> bool:
    return bool(
        page.query_id == query_id
        and page.result_id == result_id
        and context.current_result_id == result_id
        and context.current_page_index == page.page_index
        and page.schema_snapshot_id == context.prepared.binding.schema_snapshot_id
        and page.source_version == context.prepared.binding.source_version
        and page.permission_fingerprint == context.prepared.binding.permission_fingerprint
    )


async def create_database_export_task(
    session: AsyncSession,
    identity: CurrentIdentity,
    *,
    conversation_id: int,
    query_id: str,
    result_id: str | None,
    scope: str,
    file_name: str | None,
    idempotency_key: str,
    file_format: str = "xlsx",
    row_limit: int | None = None,
    page_store: DatabasePageStore,
    query_store: DatabaseQueryContextStore,
    runtime_store: ExportRuntimePlanStore,
    plan_owner: Any | None = None,
) -> GenerationTask:
    if (
        scope not in {"current_page", "all_matching", "first_n"}
        or file_format not in {"xlsx", "csv"}
        or not idempotency_key
    ):
        raise APIError(ErrorCode.VALIDATION_ERROR)
    if scope == "first_n":
        if (
            isinstance(row_limit, bool)
            or not isinstance(row_limit, int)
            or not 1 <= row_limit <= 10_000
        ):
            raise APIError(ErrorCode.VALIDATION_ERROR)
    elif row_limit is not None:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    await conversation(session, identity, conversation_id)
    fingerprint = hashlib.sha256(
        _canonical(
            {
                "conversation_id": conversation_id,
                "query_id": query_id,
                "result_id": result_id,
                "scope": scope,
                "row_limit": row_limit,
                "format": file_format,
                "file_name": file_name,
            }
        )
    ).hexdigest()
    existing = await session.scalar(
        select(GenerationTask).where(
            GenerationTask.owner_user_id == identity.user.id,
            GenerationTask.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if not hmac.compare_digest(existing.request_fingerprint, fingerprint):
            raise APIError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
        return existing

    context = query_store.get(
        query_id,
        owner_user_id=identity.user.id,
        conversation_id=conversation_id,
    )
    if context is None:
        raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
    page: DatabasePage | None = None
    if scope == "current_page":
        if result_id is None:
            raise APIError(ErrorCode.VALIDATION_ERROR)
        page = page_store.get(
            result_id,
            owner_user_id=identity.user.id,
            conversation_id=conversation_id,
        )
        if page is None or not _page_matches_context(page, query_id, result_id, context):
            raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
    elif result_id is not None:
        raise APIError(ErrorCode.VALIDATION_ERROR)

    await revalidate_schema(
        session,
        identity,
        context.prepared.binding,
        context.guarded_query.referenced_relations,
    )
    expires_at = datetime.now(UTC) + timedelta(seconds=EXPORT_PLAN_TTL_SECONDS)
    runtime_handle = runtime_store.create_handle()
    source_refs = (
        {
            "source_type": "database",
            "source_title": context.prepared.binding.source_name,
            "database_name": context.prepared.binding.database_name,
            "database_source_id": str(context.prepared.binding.database_source_id),
            "schema_snapshot_id": str(context.prepared.binding.schema_snapshot_id),
            "source_version": context.prepared.binding.source_version,
            "permission_fingerprint": context.prepared.binding.permission_fingerprint,
            "referenced_relations": list(context.guarded_query.referenced_relations),
            "scope": scope,
            "conditions_summary": context.conditions_summary,
            "queried_at": page.queried_at.isoformat() if page else None,
        },
    )
    runtime = ExportRuntimePlan(
        handle=runtime_handle,
        owner_user_id=identity.user.id,
        conversation_id=conversation_id,
        scope=scope,
        binding=context.prepared.binding,
        catalog=context.prepared.catalog,
        guarded_query=context.guarded_query,
        page=page,
        source_refs=source_refs,
        expires_at=time.monotonic() + EXPORT_PLAN_TTL_SECONDS,
        row_limit=row_limit,
    )
    export_plan = DatabaseExportPlan(
        owner_user_id=identity.user.id,
        conversation_id=conversation_id,
        query_id=query_id,
        result_id=result_id,
        runtime_handle=runtime_handle,
        scope=scope,
        format=file_format,
        database_source_id=context.prepared.binding.database_source_id,
        schema_snapshot_id=context.prepared.binding.schema_snapshot_id,
        source_version=context.prepared.binding.source_version,
        permission_fingerprint=context.prepared.binding.permission_fingerprint,
        catalog=catalog_to_wire(context.prepared.catalog),
        referenced_relations=list(context.guarded_query.referenced_relations),
        conditions_summary=context.conditions_summary,
        expires_at=expires_at,
    )
    session.add(export_plan)
    await session.flush()
    task = GenerationTask(
        owner_user_id=identity.user.id,
        conversation_id=conversation_id,
        kind="database_export",
        format=file_format,
        scope=scope,
        export_plan_id=export_plan.id,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        input_ref={
            "runtime_handle": runtime_handle,
            "file_name": file_name,
            "row_limit": row_limit,
        },
        source_refs=list(source_refs),
        status="queued",
        total_rows=page.returned_rows if page else None,
    )
    session.add(task)
    await session.flush()
    session.add(
        OutboxEvent(
            aggregate_type="generation_task",
            aggregate_id=task.id,
            event_type="generation.export.requested",
            payload={"task_id": str(task.id)},
            deduplication_key=f"generation:{task.id}",
        )
    )
    runtime_store.put(runtime)
    if plan_owner is not None:
        register = getattr(plan_owner, "register_export_plan", None)
        if not callable(register):
            raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE)
        # The independent MCP keeps this request-local plan in bounded memory;
        # the durable task still contains only the opaque runtime handle.
        await register(runtime)
    await session.flush()
    return task


async def create_chart_task(
    session: AsyncSession,
    identity: CurrentIdentity,
    *,
    conversation_id: int,
    query_id: str,
    result_id: str | None,
    chart_type: str,
    chart_scope: str,
    chart_format: str,
    title: str,
    dimension_column: str,
    metric_column: str,
    statistic: str,
    dimension_kind: str = "category",
    sort_order: str = "input",
    top_n: int | None = None,
    file_name: str | None = None,
    idempotency_key: str,
    page_store: DatabasePageStore,
    query_store: DatabaseQueryContextStore,
    runtime_store: ExportRuntimePlanStore,
    plan_owner: Any | None = None,
) -> GenerationTask:
    """Freeze a chart request from the current guarded query context.

    The durable row contains only chart intent and provenance.  SQL, bound
    values and result rows stay in the request-local plan and, when split, in
    the independent MCP owner.
    """

    if (
        chart_type not in CHART_TYPES
        or chart_scope not in CHART_SCOPES
        or chart_format not in CHART_FORMATS
        or not idempotency_key
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(statistic, str)
        or not statistic.strip()
        or not _CHART_IDENTIFIER_RE.fullmatch(dimension_column)
        or not _CHART_IDENTIFIER_RE.fullmatch(metric_column)
        or dimension_kind not in {"category", "time"}
        or sort_order not in {"input", "ascending", "descending"}
    ):
        raise APIError(ErrorCode.VALIDATION_ERROR)
    if chart_scope == "top_n":
        if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 100:
            raise APIError(ErrorCode.VALIDATION_ERROR)
        # The renderer has a chart-type-specific point budget.  Reject at task
        # creation so an over-budget request cannot be queued and later fail
        # after a worker has consumed the source stream.
        _validate_chart_point_budget(chart_type, chart_scope, top_n)
    elif top_n is not None:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    if chart_scope == "current_page" and not result_id:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    if chart_scope != "current_page" and result_id is not None:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    await conversation(session, identity, conversation_id)
    context = query_store.get(
        query_id,
        owner_user_id=identity.user.id,
        conversation_id=conversation_id,
    )
    if context is None:
        raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
    if chart_scope in {"all_aggregated", "top_n"} and (
        context.query_kind != "aggregate" or context.has_more
    ):
        raise APIError(ErrorCode.QUERY_RESULT_TOO_LARGE)
    page: DatabasePage | None = None
    if chart_scope == "current_page":
        page = page_store.get(
            result_id,
            owner_user_id=identity.user.id,
            conversation_id=conversation_id,
        )
        if page is None or not _page_matches_context(page, query_id, result_id or "", context):
            raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
    await revalidate_schema(
        session,
        identity,
        context.prepared.binding,
        context.guarded_query.referenced_relations,
    )
    statistic = _derived_chart_statistic(context, dimension_column, metric_column)
    # Idempotency binds the server-derived statistic, never the caller's
    # placeholder or model wording.
    fingerprint = hashlib.sha256(
        _canonical(
            {
                "conversation_id": conversation_id,
                "query_id": query_id,
                "result_id": result_id,
                "chart_type": chart_type,
                "chart_scope": chart_scope,
                "chart_format": chart_format,
                "title": title.strip(),
                "dimension_column": dimension_column,
                "metric_column": metric_column,
                "statistic": statistic,
                "dimension_kind": dimension_kind,
                "sort_order": sort_order,
                "top_n": top_n,
                "file_name": file_name,
            }
        )
    ).hexdigest()
    existing = await session.scalar(
        select(GenerationTask).where(
            GenerationTask.owner_user_id == identity.user.id,
            GenerationTask.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if not hmac.compare_digest(existing.request_fingerprint, fingerprint):
            raise APIError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
        return existing
    # The MCP export scope is an internal transport detail.  The chart task
    # keeps the user-visible top_n/all_aggregated contract separately.
    export_scope = {
        "current_page": "current_page",
        "all_aggregated": "all_matching",
        "top_n": "first_n",
    }[chart_scope]
    row_limit = top_n if chart_scope == "top_n" else None
    runtime_handle = runtime_store.create_handle()
    source_refs = (
        {
            "source_type": "database",
            "source_title": context.prepared.binding.source_name,
            "database_name": context.prepared.binding.database_name,
            "database_source_id": str(context.prepared.binding.database_source_id),
            "schema_snapshot_id": str(context.prepared.binding.schema_snapshot_id),
            "source_version": context.prepared.binding.source_version,
            "permission_fingerprint": context.prepared.binding.permission_fingerprint,
            "referenced_relations": list(context.guarded_query.referenced_relations),
            "scope": chart_scope,
            "query_kind": context.query_kind,
            "conditions_summary": context.conditions_summary,
            "queried_at": page.queried_at.isoformat() if page else None,
        },
    )
    runtime = ExportRuntimePlan(
        handle=runtime_handle,
        owner_user_id=identity.user.id,
        conversation_id=conversation_id,
        scope=export_scope,
        binding=context.prepared.binding,
        catalog=context.prepared.catalog,
        guarded_query=context.guarded_query,
        page=page,
        source_refs=source_refs,
        expires_at=time.monotonic() + EXPORT_PLAN_TTL_SECONDS,
        row_limit=row_limit,
    )
    export_plan = DatabaseExportPlan(
        owner_user_id=identity.user.id,
        conversation_id=conversation_id,
        query_id=query_id,
        result_id=result_id,
        runtime_handle=runtime_handle,
        scope=export_scope,
        format="xlsx",
        database_source_id=context.prepared.binding.database_source_id,
        schema_snapshot_id=context.prepared.binding.schema_snapshot_id,
        source_version=context.prepared.binding.source_version,
        permission_fingerprint=context.prepared.binding.permission_fingerprint,
        catalog=catalog_to_wire(context.prepared.catalog),
        referenced_relations=list(context.guarded_query.referenced_relations),
        conditions_summary=context.conditions_summary,
        expires_at=datetime.now(UTC) + timedelta(seconds=EXPORT_PLAN_TTL_SECONDS),
    )
    session.add(export_plan)
    await session.flush()
    chart_input = {
        "runtime_handle": runtime_handle,
        "file_name": file_name,
        # One frozen stream is rendered into all four formats.  The selected
        # task.format is the artifact published by this task; the renderer
        # receives the same snapshot for every format and never re-queries.
        "formats": ["png", "pdf", "xlsx", "csv"],
        "query_id": query_id,
        "result_id": result_id,
        "chart": {
            "chart_type": chart_type,
            "title": title.strip(),
            "scope": chart_scope,
            "dimension_column": dimension_column,
            "metric_column": metric_column,
            "statistic": statistic,
            "dimension_kind": dimension_kind,
            "sort_order": sort_order,
            "top_n": top_n,
        },
    }
    task = GenerationTask(
        owner_user_id=identity.user.id,
        conversation_id=conversation_id,
        kind="chart_render",
        format=chart_format,
        # The durable task scope uses the existing export transport contract;
        # the user-facing chart scope remains frozen in input_ref["chart"].
        scope=export_scope,
        export_plan_id=export_plan.id,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        input_ref=chart_input,
        source_refs=list(source_refs),
        status="queued",
        total_rows=page.returned_rows if page else None,
    )
    session.add(task)
    await session.flush()
    session.add(
        OutboxEvent(
            aggregate_type="generation_task",
            aggregate_id=task.id,
            event_type="generation.export.requested",
            payload={"task_id": str(task.id)},
            deduplication_key=f"generation:{task.id}",
        )
    )
    runtime_store.put(runtime)
    if plan_owner is not None:
        register = getattr(plan_owner, "register_export_plan", None)
        if not callable(register):
            raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE)
        await register(runtime)
    await session.flush()
    return task


async def get_task(
    session: AsyncSession, identity: CurrentIdentity, task_id: int, *, lock: bool = False
) -> GenerationTask:
    query = select(GenerationTask).where(
        GenerationTask.id == task_id,
        GenerationTask.owner_user_id == identity.user.id,
    )
    task = await session.scalar(query.with_for_update() if lock else query)
    if task is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    return task


async def cancel_task(
    session: AsyncSession, identity: CurrentIdentity, task_id: int
) -> GenerationTask:
    task = await get_task(session, identity, task_id, lock=True)
    if task.status in {"succeeded", "failed", "cancelled"}:
        return task
    task.status, task.phase = "cancelled", "cancelled"
    task.finished_at = task.updated_at = datetime.now(UTC)
    task.execution_owner = None
    task.execution_lease_expires_at = None
    await session.flush()
    return task


__all__ = [
    "SUPPORTED_FORMATS",
    "CHART_FORMATS",
    "cancel_task",
    "create_draft",
    "create_database_export_task",
    "create_chart_task",
    "create_render_task",
    "draft_hash",
    "draft_output",
    "get_draft",
    "get_task",
    "rebind_generated_sources",
    "revalidate_generated_sources",
    "task_output",
    "update_draft",
]
