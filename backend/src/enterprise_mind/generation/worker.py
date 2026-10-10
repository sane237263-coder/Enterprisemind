"""Worker for frozen content-render and database-export tasks."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_mind.agent.generation_models import GeneratedFile, GenerationTask
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.charts.models import (
    ChartFormat,
    ChartPoint,
    ChartSnapshot,
    ChartSpec,
    ChartValidationError,
)
from enterprise_mind.charts.rendering import render_chart
from enterprise_mind.database.catalog import catalog_from_wire
from enterprise_mind.database.export_models import DatabaseExportPlan
from enterprise_mind.database.export_runtime import ExportRuntimePlan, ExportRuntimePlanStore
from enterprise_mind.database.mcp_client import DatabaseMCPClient
from enterprise_mind.database.models import DatabaseSource, SchemaSnapshot
from enterprise_mind.database.projection import BoundSchema, revalidate_schema
from enterprise_mind.generation import service as generation_service
from enterprise_mind.generation.rendering import (
    RenderInput,
    await_thread_completion,
    render_export_csv,
    render_export_xlsx,
    render_to_path,
)
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.security import parse_id
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.infrastructure.settings import Phase9Config, WorkerConfig
from enterprise_mind.ingestion.models import OutboxEvent, StorageObject
from enterprise_mind.ingestion.storage import ObjectStorage

SAFE_FAILURES = {
    ErrorCode.FILE_TOO_LARGE: "生成文件超过当前大小预算。",
    ErrorCode.FILE_CONTENT_MISMATCH: "生成文件未通过格式完整性检查。",
    ErrorCode.FILE_GENERATION_TIMEOUT: "文件生成超过时间预算。",
    ErrorCode.UNSUPPORTED_FILE_TYPE: "当前格式不支持生成。",
    ErrorCode.VALIDATION_ERROR: "冻结内容不符合生成格式要求。",
    ErrorCode.STORAGE_UNAVAILABLE: "文件存储暂时不可用。",
    ErrorCode.QUERY_CONTEXT_EXPIRED: "数据库导出计划已过期，请重新查询后导出。",
    ErrorCode.DATABASE_SOURCE_UNAVAILABLE: "企业数据源暂时不可用。",
    ErrorCode.DATABASE_RELATION_FORBIDDEN: "数据库权限已变化，导出未发布。",
    ErrorCode.DATABASE_QUERY_REJECTED: "数据库导出查询未通过安全校验。",
    ErrorCode.DATABASE_QUERY_TIMEOUT: "数据库导出超过时间预算。",
    ErrorCode.QUERY_RESULT_TOO_LARGE: "数据库导出超过当前行数或数据预算。",
}


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


class _ExecutionLeaseLost(Exception):
    """The handler no longer owns the durable generation attempt."""


class _TaskCancelled(Exception):
    """The request owner cancelled this task while its worker was running."""


class GenerationWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        storage: ObjectStorage | None,
        config: Phase9Config,
        *,
        worker_config: WorkerConfig | None = None,
        database: DatabaseMCPClient | None = None,
        export_plans: ExportRuntimePlanStore | None = None,
    ) -> None:
        self.sessions = sessions
        self.storage = storage
        self.config = config
        self.worker_config = worker_config or WorkerConfig()
        self.database = database
        self.export_plans = export_plans

    async def _set_failed(
        self, task_id: int, execution_owner: str, code: str, message: str
    ) -> bool:
        async with self.sessions() as session, session.begin():
            task = await session.scalar(
                select(GenerationTask).where(GenerationTask.id == task_id).with_for_update()
            )
            now = datetime.now(UTC)
            if (
                task is None
                or task.status != "running"
                or task.execution_owner != execution_owner
                or task.execution_lease_expires_at is None
                or task.execution_lease_expires_at <= now
            ):
                return False
            task.status, task.phase = "failed", "failed"
            task.error_code, task.error_message = code, message
            task.finished_at = task.updated_at = now
            task.execution_lease_expires_at = None
            return True

    async def _renew_execution_lease(self, task_id: int, execution_owner: str) -> bool:
        async with self.sessions() as session, session.begin():
            task = await session.scalar(
                select(GenerationTask).where(GenerationTask.id == task_id).with_for_update()
            )
            now = datetime.now(UTC)
            if (
                task is None
                or task.status != "running"
                or task.execution_owner != execution_owner
                or task.execution_lease_expires_at is None
                or task.execution_lease_expires_at <= now
            ):
                return False
            task.execution_lease_expires_at = now + timedelta(
                seconds=self.worker_config.lease_seconds
            )
            return True

    async def _execution_lease_is_active(self, task_id: int, execution_owner: str) -> bool:
        async with self.sessions() as session:
            task = await session.get(GenerationTask, task_id)
            return bool(
                task is not None
                and task.status == "running"
                and task.execution_owner == execution_owner
                and task.execution_lease_expires_at is not None
                and task.execution_lease_expires_at > datetime.now(UTC)
            )

    async def _ensure_execution_lease(
        self, task_id: int, execution_owner: str, lease_lost: asyncio.Event
    ) -> None:
        if lease_lost.is_set():
            raise _ExecutionLeaseLost
        async with self.sessions() as session:
            task = await session.get(GenerationTask, task_id)
        if task is not None and task.status == "cancelled":
            raise _TaskCancelled
        now = datetime.now(UTC)
        if (
            task is None
            or task.status != "running"
            or task.execution_owner != execution_owner
            or task.execution_lease_expires_at is None
            or task.execution_lease_expires_at <= now
        ):
            lease_lost.set()
            raise _ExecutionLeaseLost

    @staticmethod
    async def _drain_cancelled_task(task: asyncio.Task[Any]) -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                if not task.done():
                    raise
        await asyncio.gather(task, return_exceptions=True)

    async def _run_with_execution_lease(
        self,
        operation: Coroutine[Any, Any, Any],
        lease_lost: asyncio.Event,
    ) -> Any:
        work = asyncio.create_task(operation)
        lost = asyncio.create_task(lease_lost.wait())
        try:
            done, _ = await asyncio.wait({work, lost}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                return work.result()
            if lost in done and lease_lost.is_set():
                work.cancel()
                await self._drain_cancelled_task(work)
                raise _ExecutionLeaseLost
            return work.result()
        except asyncio.CancelledError:
            work.cancel()
            await self._drain_cancelled_task(work)
            raise
        finally:
            lost.cancel()
            await asyncio.gather(lost, return_exceptions=True)

    async def _renew_execution_lease_until_cancelled(
        self, task_id: int, execution_owner: str, lease_lost: asyncio.Event
    ) -> None:
        while True:
            await asyncio.sleep(self.worker_config.lease_renew_interval_seconds)
            try:
                if not await self._renew_execution_lease(task_id, execution_owner):
                    lease_lost.set()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not let a worker publish if it cannot prove the lease is
                # still current. The next queue delivery may reclaim it later.
                lease_lost.set()
                return

    async def _expire_execution_lease_for_retry(self, task_id: int, execution_owner: str) -> bool:
        async with self.sessions() as session, session.begin():
            task = await session.scalar(
                select(GenerationTask).where(GenerationTask.id == task_id).with_for_update()
            )
            now = datetime.now(UTC)
            if (
                task is None
                or task.status != "running"
                or task.execution_owner != execution_owner
                or task.execution_lease_expires_at is None
                or task.execution_lease_expires_at <= now
            ):
                return False
            task.execution_lease_expires_at = now
            return True

    async def _task_is_terminal(self, task_id: int) -> bool:
        async with self.sessions() as session:
            task = await session.get(GenerationTask, task_id)
            return task is not None and task.status in {
                "succeeded",
                "failed",
                "cancelled",
            }

    async def recover_database_exports(self) -> None:
        """Close process-local export tasks that cannot survive an app restart."""

        if self.export_plans is None:
            return
        async with self.sessions() as session, session.begin():
            tasks = list(
                (
                    await session.scalars(
                        select(GenerationTask)
                        .where(
                            GenerationTask.kind.in_(("database_export", "chart_render")),
                            GenerationTask.status.in_(("queued", "running")),
                        )
                        .with_for_update()
                        .limit(256)
                    )
                ).all()
            )
            now = datetime.now(UTC)
            for task in tasks:
                frozen = dict(task.input_ref or {})
                handle = frozen.get("runtime_handle")
                if (
                    self.export_plans.get(
                        handle,
                        owner_user_id=task.owner_user_id,
                        conversation_id=task.conversation_id or 0,
                    )
                    is not None
                ):
                    continue
                # In the deployed split topology the independent MCP owns
                # the process-local plan.  An API restart must not infer that
                # its fresh local dictionary invalidated a still-live owner;
                # the queue Worker will ask that owner and fail safely if it
                # was restarted as well.
                if self.database is not None:
                    continue
                if (
                    task.status == "running"
                    and task.execution_lease_expires_at is not None
                    and task.execution_lease_expires_at > now
                ):
                    continue
                task.status, task.phase = "failed", "failed"
                task.error_code = ErrorCode.QUERY_CONTEXT_EXPIRED.value
                task.error_message = SAFE_FAILURES[ErrorCode.QUERY_CONTEXT_EXPIRED]
                task.finished_at = task.updated_at = now
                task.execution_owner = None
                task.execution_lease_expires_at = None
                if task.export_plan_id is not None:
                    plan = await session.get(
                        DatabaseExportPlan, task.export_plan_id, with_for_update=True
                    )
                    if plan is not None:
                        await session.delete(plan)
                event = await session.scalar(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.aggregate_type == "generation_task",
                        OutboxEvent.aggregate_id == task.id,
                        OutboxEvent.event_type == "generation.export.requested",
                    )
                    .with_for_update()
                )
                if event is not None:
                    event.status = "published"
                    event.published_at = now
                    event.lease_owner = event.lease_expires_at = None
                    event.last_error = None

    async def _set_progress(
        self, task_id: int, rows: int, *, execution_owner: str | None = None
    ) -> bool:
        if execution_owner is None:
            return False
        async with self.sessions() as session, session.begin():
            task = await session.scalar(
                select(GenerationTask).where(GenerationTask.id == task_id).with_for_update()
            )
            now = datetime.now(UTC)
            if (
                task is None
                or task.status != "running"
                or task.execution_owner != execution_owner
                or task.execution_lease_expires_at is None
                or task.execution_lease_expires_at <= now
            ):
                return False
            task.processed_rows = rows
            task.updated_at = now
            return True

    async def _cancelled(self, task_id: int) -> bool:
        async with self.sessions() as session:
            task = await session.get(GenerationTask, task_id)
            return task is not None and task.status == "cancelled"

    async def _identity_for_user(self, user_id: int) -> CurrentIdentity:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(User, Department)
                    .outerjoin(Department, User.department_id == Department.id)
                    .where(User.id == user_id)
                )
            ).one_or_none()
            if row is None:
                raise APIError(ErrorCode.AUTH_REQUIRED)
            identity = CurrentIdentity(row[0], row[1])
            AuthorizationService.require_active(identity)
            return identity

    async def _revalidate_export(self, plan: ExportRuntimePlan) -> None:
        identity = await self._identity_for_user(plan.owner_user_id)
        async with self.sessions() as session:
            await revalidate_schema(
                session,
                identity,
                plan.binding,
                plan.guarded_query.referenced_relations,
            )

    async def _load_and_revalidate_durable_export(
        self, task: GenerationTask, handle: str
    ) -> DatabaseExportPlan:
        """Recheck current authorization before asking the remote owner to stream."""

        if task.export_plan_id is None:
            raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
        async with self.sessions() as session:
            plan = await session.get(DatabaseExportPlan, task.export_plan_id)
            if (
                plan is None
                or plan.runtime_handle != handle
                or plan.format != ("xlsx" if task.kind == "chart_render" else task.format)
                or plan.expires_at <= datetime.now(UTC)
            ):
                raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
            source = await session.get(DatabaseSource, plan.database_source_id)
            snapshot = await session.get(SchemaSnapshot, plan.schema_snapshot_id)
            if source is None or snapshot is None:
                raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
            identity = await self._identity_for_user(task.owner_user_id)
            binding = BoundSchema(
                database_source_id=plan.database_source_id,
                source_name=source.name,
                database_name=source.database_name,
                schema_snapshot_id=plan.schema_snapshot_id,
                snapshot_no=snapshot.snapshot_no,
                source_version=plan.source_version,
                catalog=catalog_from_wire(plan.catalog),
                permission_fingerprint=plan.permission_fingerprint,
            )
            await revalidate_schema(session, identity, binding, tuple(plan.referenced_relations))
            return plan

    async def _export_frames(
        self, plan: ExportRuntimePlan, task_id: int
    ) -> AsyncIterator[dict[str, Any]]:
        if plan.scope == "current_page":
            page = plan.page
            if page is None:
                raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
            columns = [dict(column) for column in page.columns]
            raw_names = [column.get("name") for column in columns]
            if not raw_names or any(not isinstance(name, str) or not name for name in raw_names):
                raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
            names = [name for name in raw_names if isinstance(name, str)]
            yield {
                "type": "header",
                "columns": columns,
                "referenced_relations": list(plan.guarded_query.referenced_relations),
            }
            rows: list[list[Any]] = []
            for item in page.rows:
                if await self._cancelled(task_id):
                    raise _TaskCancelled
                if not isinstance(item, dict) or any(name not in item for name in names):
                    raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
                rows.append([item[name] for name in names])
            if rows:
                yield {"type": "batch", "sequence": 1, "rows": rows}
            yield {
                "type": "end",
                "exported_rows": len(rows),
                "complete": True,
                "executed_at": page.queried_at.isoformat(),
            }
            return
        if self.database is None:
            raise APIError(ErrorCode.DATABASE_SOURCE_UNAVAILABLE)
        query = plan.guarded_query
        if plan.scope == "first_n":
            if plan.row_limit is None:
                raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
            query = replace(query, export_limit=plan.row_limit)
        async for frame in self.database.stream_export(
            query,
            plan.catalog,
            plan.binding.schema_snapshot_id,
            max_rows=self.config.export_max_rows,
            batch_size=self.config.export_fetch_batch_size,
            max_serialized_bytes=self.config.export_max_serialized_bytes,
            timeout_seconds=self.config.database_export_timeout_seconds,
        ):
            if await self._cancelled(task_id):
                raise _TaskCancelled
            yield frame

    async def _cleanup_export_plan(
        self, task_id: int, handle: str | None, execution_owner: str | None
    ) -> None:
        if handle is None:
            return
        owner_user_id: int | None = None
        conversation_id = 0
        async with self.sessions() as session, session.begin():
            task = await session.scalar(
                select(GenerationTask).where(GenerationTask.id == task_id).with_for_update()
            )
            if task is None or task.status not in {"succeeded", "failed", "cancelled"}:
                return
            frozen = dict(task.input_ref or {})
            if frozen.get("runtime_handle") != handle:
                return
            # Terminal rows cannot be reclaimed. Keep the attempt token on
            # worker-produced terminal rows so delayed cleanup is fenced to
            # that attempt; API cancellation clears it and is fenced by the
            # terminal state plus both lease fields being empty.
            if (
                task.execution_owner != execution_owner
                or task.execution_lease_expires_at is not None
            ):
                return
            owner_user_id = task.owner_user_id
            conversation_id = task.conversation_id or 0
            if task.export_plan_id is not None:
                plan = await session.get(
                    DatabaseExportPlan, task.export_plan_id, with_for_update=True
                )
                if plan is not None:
                    await session.delete(plan)
        if self.database is not None:
            await self.database.release_export_plan(
                handle,
                owner_user_id=owner_user_id,
                conversation_id=conversation_id,
            )
        if self.export_plans is not None:
            self.export_plans.remove(handle)

    async def _cleanup_terminal_export_plan(self, task_id: int, handle: str | None = None) -> bool:
        """Retry shared-plan cleanup only after the task has become terminal."""

        async with self.sessions() as session:
            task = await session.get(GenerationTask, task_id)
            if task is None or task.status not in {"succeeded", "failed", "cancelled"}:
                return False
            frozen = dict(task.input_ref or {})
            terminal_handle = frozen.get("runtime_handle")
            if handle is not None and terminal_handle != handle:
                return False
            owner = task.execution_owner
        try:
            await self._cleanup_export_plan(task_id, terminal_handle, owner)
        except Exception:
            return False
        return True

    async def _render(
        self,
        task: GenerationTask,
        frozen: dict[str, Any],
        directory: Path,
        *,
        execution_owner: str | None = None,
    ) -> Any:
        if task.kind == "content_render":
            identity = await self._identity_for_user(task.owner_user_id)
            async with self.sessions() as session:
                await generation_service.revalidate_generated_sources(
                    session, identity, list(task.source_refs or [])
                )
            render_input = RenderInput(
                title=str(frozen.get("title", "企业内容")),
                body_markdown=str(frozen.get("body_markdown", "")),
                tables=tuple(frozen.get("tables") or ()),
                source_refs=tuple(frozen.get("source_refs") or ()),
            )
            return await await_thread_completion(
                render_to_path,
                render_input,
                task.format,
                directory,
                frozen.get("file_name"),
                max_bytes=self.config.generation_max_file_bytes,
            )
        if task.kind not in {"database_export", "chart_render"}:
            raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
        handle = frozen.get("runtime_handle")
        if not isinstance(handle, str):
            raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
        plan: Any
        source_refs: Any
        if self.export_plans is not None:
            local_plan = self.export_plans.get(
                handle,
                owner_user_id=task.owner_user_id,
                conversation_id=task.conversation_id or 0,
            )
            if local_plan is None:
                raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
            plan = local_plan
            await self._revalidate_export(local_plan)
            frames = self._export_frames(local_plan, task.id)
            source_refs = local_plan.source_refs

            async def revalidate() -> None:
                await self._revalidate_export(local_plan)

        elif self.database is not None:
            durable_plan = await self._load_and_revalidate_durable_export(task, handle)
            plan = durable_plan
            database = self.database

            async def remote_frames() -> AsyncIterator[dict[str, Any]]:
                async for frame in database.stream_export_plan(
                    handle,
                    owner_user_id=task.owner_user_id,
                    conversation_id=task.conversation_id or 0,
                    max_rows=self.config.export_max_rows,
                    batch_size=self.config.export_fetch_batch_size,
                    max_serialized_bytes=self.config.export_max_serialized_bytes,
                    timeout_seconds=self.config.database_export_timeout_seconds,
                ):
                    if await self._cancelled(task.id):
                        raise _TaskCancelled
                    yield frame

            frames = remote_frames()
            source_refs = list(task.source_refs or [])
            # The owner rechecks the external source; the Worker rechecks the
            # application authorization before and after consuming the stream.

            async def revalidate() -> None:
                await self._load_and_revalidate_durable_export(task, handle)

        else:
            raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
        if task.kind == "chart_render":
            rendered = await self._render_chart(
                task,
                frozen,
                directory,
                frames,
                plan,
                source_refs,
                execution_owner=execution_owner,
            )
            await revalidate()
            return rendered

        last_progress = 0

        async def progress(rows: int) -> None:
            nonlocal last_progress
            if rows == 0 or rows - last_progress >= self.config.export_fetch_batch_size:
                last_progress = rows
                if execution_owner is None:
                    await self._set_progress(task.id, rows)
                else:
                    updated = await self._set_progress(
                        task.id, rows, execution_owner=execution_owner
                    )
                    if not updated:
                        raise _ExecutionLeaseLost

        if task.format not in {"xlsx", "csv"}:
            raise APIError(ErrorCode.UNSUPPORTED_FILE_TYPE)
        render_export = render_export_csv if task.format == "csv" else render_export_xlsx
        rendered = await render_export(
            frames,
            directory,
            frozen.get("file_name"),
            max_rows=self.config.export_max_rows,
            max_bytes=self.config.generation_max_file_bytes,
            progress_callback=progress,
            **({"source_refs": source_refs} if task.format == "xlsx" else {}),
        )
        await revalidate()
        return rendered

    async def _render_chart(
        self,
        task: GenerationTask,
        frozen: dict[str, Any],
        directory: Path,
        frames: AsyncIterator[dict[str, Any]],
        plan: Any,
        source_refs: Any,
        *,
        execution_owner: str | None = None,
    ) -> Any:
        """Consume one authorized MCP stream into one four-format chart bundle."""

        chart = frozen.get("chart")
        if not isinstance(chart, dict):
            raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
        chart_type = chart.get("chart_type")
        scope = chart.get("scope")
        if chart_type not in {"bar", "line", "pie"} or scope not in {
            "current_page",
            "all_aggregated",
            "top_n",
        }:
            raise APIError(ErrorCode.VALIDATION_ERROR)
        maximum = {"bar": 50, "line": 100, "pie": 20}[chart_type]
        columns: list[str] | None = None
        referenced_relations: tuple[str, ...] | None = None
        rows: list[dict[str, Any]] = []
        expected_sequence = 1
        executed_at: str | None = None
        saw_end = False
        async for frame in frames:
            if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
                raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
            kind = frame["type"]
            if kind == "header":
                if columns is not None or saw_end:
                    raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
                raw_columns = frame.get("columns")
                relations = frame.get("referenced_relations")
                if (
                    not isinstance(raw_columns, list)
                    or not raw_columns
                    or not isinstance(relations, list)
                    or any(not isinstance(item, str) for item in relations)
                ):
                    raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
                names: list[str] = []
                for column in raw_columns:
                    if not isinstance(column, dict) or not isinstance(column.get("name"), str):
                        raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
                    name = column["name"]
                    if not name or name in names:
                        raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
                    names.append(name)
                columns = names
                referenced_relations = tuple(relations)
                expected_relations = tuple(
                    getattr(plan, "referenced_relations", ())
                    or getattr(getattr(plan, "guarded_query", None), "referenced_relations", ())
                )
                if not expected_relations or referenced_relations != expected_relations:
                    raise APIError(ErrorCode.DATABASE_RELATION_FORBIDDEN)
            elif kind == "batch":
                if columns is None or saw_end or frame.get("sequence") != expected_sequence:
                    raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
                raw_rows = frame.get("rows")
                if not isinstance(raw_rows, list):
                    raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
                for raw_row in raw_rows:
                    if not isinstance(raw_row, list) or len(raw_row) != len(columns):
                        raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
                    rows.append(dict(zip(columns, raw_row, strict=True)))
                    if len(rows) > maximum:
                        raise APIError(ErrorCode.QUERY_RESULT_TOO_LARGE)
                expected_sequence += 1
            elif kind == "end":
                if (
                    columns is None
                    or saw_end
                    or frame.get("complete") is not True
                    or frame.get("exported_rows") != len(rows)
                    or not isinstance(frame.get("executed_at"), str)
                ):
                    raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
                executed_at = frame["executed_at"]
                saw_end = True
            else:
                raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
        if columns is None or not saw_end or executed_at is None:
            raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
        if await self._cancelled(task.id):
            raise _TaskCancelled
        if execution_owner is None:
            await self._set_progress(task.id, len(rows))
        else:
            updated = await self._set_progress(task.id, len(rows), execution_owner=execution_owner)
            if not updated:
                raise _ExecutionLeaseLost
        try:
            snapshot = self._chart_snapshot(
                task,
                frozen,
                plan,
                rows,
                referenced_relations or (),
                executed_at,
                source_refs,
            )
            raw_formats = frozen.get("formats")
            if (
                not isinstance(raw_formats, list)
                or set(raw_formats) != {"png", "pdf", "xlsx", "csv"}
                or task.format not in raw_formats
            ):
                # A chart task is one frozen snapshot and one bundle.  Never
                # silently fall back to rendering only the requested format;
                # that would make the other downloads require a re-query.
                raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
            formats: list[ChartFormat] = ["png", "pdf", "xlsx", "csv"]
            artifacts = await await_thread_completion(render_chart, snapshot, formats, directory)
        except ChartValidationError as error:
            # The renderer never receives SQL or caller paths.  A failed
            # snapshot is a safe validation failure and is never published.
            raise APIError(ErrorCode.VALIDATION_ERROR) from error
        selected = artifacts.get(cast(ChartFormat, task.format))
        if selected is None or set(artifacts) != {"png", "pdf", "xlsx", "csv"}:
            raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
        from enterprise_mind.generation.rendering import RenderedFile

        rendered_artifacts = tuple(
            RenderedFile(
                artifact.path,
                artifact.format,
                artifact.mime_type,
                artifact.size,
                artifact.sha256,
                len(rows),
                executed_at,
            )
            for artifact in (artifacts[format_name] for format_name in formats)
        )
        return replace(
            next(item for item in rendered_artifacts if item.file_format == task.format),
            artifacts=rendered_artifacts,
        )

    @staticmethod
    def _chart_snapshot(
        task: GenerationTask,
        frozen: dict[str, Any],
        plan: Any,
        rows: list[dict[str, Any]],
        referenced_relations: tuple[str, ...],
        executed_at: str,
        source_refs: Any,
    ) -> ChartSnapshot:
        chart = frozen["chart"]
        source_meta = source_refs
        query_kind = None
        if (
            isinstance(source_meta, (list, tuple))
            and source_meta
            and isinstance(source_meta[0], dict)
        ):
            query_kind = source_meta[0].get("query_kind")
        if chart["scope"] in {"all_aggregated", "top_n"} and query_kind != "aggregate":
            raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
        source_id = getattr(plan, "database_source_id", None)
        snapshot_id = getattr(plan, "schema_snapshot_id", None)
        source_version = getattr(plan, "source_version", None)
        permission = str(getattr(plan, "permission_fingerprint", ""))
        if source_id is None or snapshot_id is None or source_version is None or not permission:
            binding = getattr(plan, "binding", None)
            source_id = getattr(binding, "database_source_id", source_id)
            snapshot_id = getattr(binding, "schema_snapshot_id", snapshot_id)
            source_version = getattr(binding, "source_version", source_version)
            permission = str(getattr(binding, "permission_fingerprint", permission))
        if source_id is None or snapshot_id is None or source_version is None or not permission:
            raise APIError(ErrorCode.DATABASE_RELATION_FORBIDDEN)
        dimension = chart.get("dimension_column")
        metric = chart.get("metric_column")
        if not isinstance(dimension, str) or not isinstance(metric, str):
            raise APIError(ErrorCode.VALIDATION_ERROR)
        points: list[ChartPoint] = []
        for row in rows:
            if dimension not in row or metric not in row:
                raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
            points.append(ChartPoint(str(row[dimension]), row.get(metric)))
        relation_digest = hashlib.sha256(
            "\0".join(sorted(referenced_relations)).encode()
        ).hexdigest()[:24]
        permission_digest = hashlib.sha256(permission.encode()).hexdigest()[:24]
        refs = (
            f"database-source:{source_id}:snapshot:{snapshot_id}:relations:{relation_digest}",
            f"database-result:{frozen.get('query_id')}:{frozen.get('result_id') or 'aggregate'}",
        )
        aggregate = chart["scope"] in {"all_aggregated", "top_n"}
        spec = ChartSpec(
            chart_type=chart["chart_type"],
            title=chart["title"],
            data_ref=f"database-query:{frozen.get('query_id')}",
            dimension_column=dimension,
            metric_column=metric,
            statistic=chart["statistic"],
            scope=chart["scope"],
            scope_label=(
                f"当前页（{len(points)} 行）"
                if chart["scope"] == "current_page"
                else (
                    f"前 {chart['top_n']} 项（服务端聚合）"
                    if chart["scope"] == "top_n"
                    else f"全部匹配数据（服务端聚合，{len(points)} 项）"
                )
            ),
            source_refs=refs,
            queried_at=executed_at,
            revision=f"schema:{snapshot_id}:source:{source_version}:permission:{permission_digest}",
            sort_order=chart.get("sort_order", "input"),
            dimension_kind=chart.get("dimension_kind", "category"),
            aggregate_complete=aggregate,
            top_n=chart.get("top_n"),
        )
        return ChartSnapshot.freeze(spec, points)

    async def handle_event(self, event_id: int) -> bool:
        task: GenerationTask | None = None
        task_id: int | None = None
        execution_owner: str | None = None
        task_kind: str | None = None
        frozen: dict[str, Any] = {}
        terminal = False
        async with self.sessions() as session, session.begin():
            event = await session.get(OutboxEvent, event_id)
            if event is None or event.event_type not in {
                "generation.requested",
                "generation.export.requested",
            }:
                return True
            try:
                task_id = parse_id(event.payload["task_id"])
            except (KeyError, TypeError, ValueError):
                return True
            task = await session.scalar(
                select(GenerationTask).where(GenerationTask.id == task_id).with_for_update()
            )
            if task is None:
                return True
            frozen = dict(task.input_ref or {})
            task_kind = task.kind
            if task.status in {"succeeded", "failed", "cancelled"}:
                terminal = True
            else:
                now = datetime.now(UTC)
                if (
                    task.status == "running"
                    and task.execution_lease_expires_at is not None
                    and task.execution_lease_expires_at > now
                ):
                    return False
                if task.status not in {"queued", "running"}:
                    return False
                if task.execution_attempt_count >= self.config.generation_recovery_max_attempts:
                    task.status, task.phase = "failed", "failed"
                    task.error_code = ErrorCode.FILE_GENERATION_TIMEOUT.value
                    task.error_message = "生成任务超过最大恢复次数，已停止处理。"
                    task.finished_at = task.updated_at = now
                    task.execution_lease_expires_at = None
                    terminal = True
                else:
                    execution_owner = secrets.token_hex(32)
                    task.status, task.phase = "running", "rendering"
                    task.execution_owner = execution_owner
                    task.execution_lease_expires_at = now + timedelta(
                        seconds=self.worker_config.lease_seconds
                    )
                    task.execution_attempt_count += 1
                    task.started_at = task.started_at or now
                    task.updated_at = now

        assert task_id is not None and task_kind is not None
        runtime_handle = frozen.get("runtime_handle")
        is_export = task_kind in {"database_export", "chart_render"}
        if terminal:
            if is_export:
                return await self._cleanup_terminal_export_plan(task_id)
            return True

        assert task is not None and execution_owner is not None
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._renew_execution_lease_until_cancelled(task_id, execution_owner, lease_lost)
        )
        uploaded_keys: list[str] = []
        published = False
        try:
            if self.storage is None:
                raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
            with TemporaryDirectory(prefix="em-generation-") as temp:
                directory = Path(temp)
                try:
                    async with asyncio.timeout(self.config.generation_timeout_seconds):
                        rendered = await self._run_with_execution_lease(
                            self._render(
                                task,
                                frozen,
                                directory,
                                execution_owner=execution_owner,
                            ),
                            lease_lost,
                        )
                except TimeoutError:
                    raise APIError(
                        ErrorCode.DATABASE_QUERY_TIMEOUT
                        if is_export
                        else ErrorCode.FILE_GENERATION_TIMEOUT
                    ) from None
                await self._ensure_execution_lease(task_id, execution_owner, lease_lost)
                if _directory_size(directory) > self.config.generation_temp_max_bytes:
                    raise APIError(ErrorCode.FILE_TOO_LARGE)
                rendered_files = rendered.artifacts or (rendered,)

                async def check_attempt() -> None:
                    await self._ensure_execution_lease(task_id, execution_owner, lease_lost)

                uploaded_keys = await self._upload_rendered_files(
                    rendered_files,
                    export=is_export,
                    task_id=task_id,
                    execution_owner=execution_owner,
                    attempt_check=check_attempt,
                )
                await self._ensure_execution_lease(task_id, execution_owner, lease_lost)
                discard_after_transaction = False
                externally_cancelled = False
                async with self.sessions() as session, session.begin():
                    current = await session.scalar(
                        select(GenerationTask).where(GenerationTask.id == task_id).with_for_update()
                    )
                    now = datetime.now(UTC)
                    if current is None:
                        discard_after_transaction = True
                    elif current.status == "cancelled":
                        externally_cancelled = True
                        discard_after_transaction = True
                    elif (
                        current.status != "running"
                        or current.execution_owner != execution_owner
                        or current.execution_lease_expires_at is None
                        or current.execution_lease_expires_at <= now
                    ):
                        discard_after_transaction = True
                    else:
                        identity = await self._identity_for_user(current.owner_user_id)
                        await generation_service.revalidate_generated_sources(
                            session, identity, list(current.source_refs or [])
                        )
                        exported_rows = rendered.exported_rows
                        if is_export and exported_rows is None:
                            raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
                        source_refs = list(current.source_refs or [])
                        if is_export and rendered.executed_at:
                            source_refs = [
                                {**source, "executed_at": rendered.executed_at}
                                for source in source_refs
                            ]
                            current.source_refs = source_refs
                        artifact_objects: dict[str, StorageObject] = {}
                        for uploaded_key, rendered_file in zip(
                            uploaded_keys, rendered_files, strict=True
                        ):
                            obj = StorageObject(
                                bucket_name=self.storage.bucket,
                                object_key=uploaded_key,
                                object_type="export" if is_export else "generated",
                                size_bytes=rendered_file.size_bytes,
                                content_type=rendered_file.mime_type,
                                content_sha256=rendered_file.content_sha256,
                                status="available",
                            )
                            session.add(obj)
                            await session.flush()
                            artifact_objects[rendered_file.file_format] = obj
                            session.add(
                                GeneratedFile(
                                    task_id=current.id,
                                    owner_user_id=current.owner_user_id,
                                    conversation_id=current.conversation_id,
                                    storage_object_id=obj.id,
                                    file_name=rendered_file.path.name,
                                    file_format=rendered_file.file_format,
                                    generation_kind=current.kind,
                                    draft_id=current.draft_id,
                                    source_message_id=current.source_message_id,
                                    source_refs=source_refs,
                                    result_scope=current.scope,
                                    exported_rows=exported_rows,
                                    content_sha256=rendered_file.content_sha256,
                                    size_bytes=rendered_file.size_bytes,
                                )
                            )
                        await session.flush()
                        selected_object = artifact_objects.get(current.format)
                        if selected_object is None:
                            raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
                        # Rendering and inserts can outlast the lease while the
                        # heartbeat waits on this row lock. Recheck immediately
                        # before the transaction commits.
                        final_now = datetime.now(UTC)
                        if (
                            current.status != "running"
                            or current.execution_owner != execution_owner
                            or current.execution_lease_expires_at is None
                            or current.execution_lease_expires_at <= final_now
                        ):
                            raise _ExecutionLeaseLost
                        current.file_id = selected_object.id
                        current.processed_rows = exported_rows or 0
                        current.exported_rows = exported_rows
                        if is_export:
                            current.total_rows = exported_rows
                        current.status, current.phase = "succeeded", "published"
                        current.finished_at = current.updated_at = final_now
                        current.execution_lease_expires_at = None
                        published = True
                if discard_after_transaction:
                    await self._remove_many(uploaded_keys)
                    uploaded_keys = []
                    if externally_cancelled:
                        raise _TaskCancelled
                    raise _ExecutionLeaseLost
                # The object keys now belong to the committed GeneratedFile rows.
                uploaded_keys = []
        except _TaskCancelled:
            await self._remove_many(uploaded_keys)
            terminal_now = await self._task_is_terminal(task_id)
            if terminal_now and is_export:
                if not await self._cleanup_terminal_export_plan(task_id, runtime_handle):
                    return False
            return terminal_now
        except _ExecutionLeaseLost:
            await self._remove_many(uploaded_keys)
            terminal_now = await self._task_is_terminal(task_id)
            if terminal_now and is_export:
                if not await self._cleanup_terminal_export_plan(task_id, runtime_handle):
                    return False
            return terminal_now
        except APIError as error:
            await self._remove_many(uploaded_keys)
            await self._set_failed(
                task_id,
                execution_owner,
                error.code.value,
                SAFE_FAILURES.get(error.code, "生成文件失败。"),
            )
            terminal_now = await self._task_is_terminal(task_id)
            if terminal_now and is_export:
                if not await self._cleanup_terminal_export_plan(task_id, runtime_handle):
                    return False
            return terminal_now
        except asyncio.CancelledError:
            await self._remove_many(uploaded_keys)
            await self._expire_execution_lease_for_retry(task_id, execution_owner)
            raise
        except Exception:
            await self._remove_many(uploaded_keys)
            await self._set_failed(task_id, execution_owner, "GENERATION_FAILED", "生成文件失败。")
            terminal_now = await self._task_is_terminal(task_id)
            if terminal_now and is_export:
                if not await self._cleanup_terminal_export_plan(task_id, runtime_handle):
                    return False
            return terminal_now
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

        if published:
            if is_export and not await self._cleanup_terminal_export_plan(task_id, runtime_handle):
                # Keep the Redis delivery pending so terminal cleanup can retry.
                return False
            return True
        return await self._task_is_terminal(task_id)

    async def _remove(self, key: str | None) -> None:
        if key and self.storage is not None:
            with suppress(Exception):
                await await_thread_completion(self.storage.remove, key)

    async def _remove_many(self, keys: list[str]) -> None:
        for key in keys:
            await self._remove(key)

    async def _upload_rendered_files(
        self,
        rendered_files: tuple[Any, ...],
        *,
        export: bool,
        task_id: int | None = None,
        execution_owner: str | None = None,
        attempt_check: Callable[[], Awaitable[None]] | None = None,
    ) -> list[str]:
        """Upload and verify a bundle, deleting earlier objects on any failure."""

        if self.storage is None:
            raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
        if (task_id is None) != (execution_owner is None):
            raise ValueError("generation upload identity requires both task and owner")
        keys: list[str] = []
        try:
            for rendered_file in rendered_files:
                if attempt_check is not None:
                    await attempt_check()
                prefix = "export" if export else "generated"
                if task_id is None:
                    # Keep the direct helper contract for renderer unit tests.
                    key = self.storage.new_key(prefix)
                else:
                    assert execution_owner is not None
                    key = self.storage.new_key(prefix, task_id=task_id, owner_token=execution_owner)
                keys.append(key)
                with rendered_file.path.open("rb") as stream:
                    await await_thread_completion(
                        self.storage.upload, key, stream, rendered_file.mime_type
                    )
                if attempt_check is not None:
                    await attempt_check()
                await await_thread_completion(
                    self.storage.verify,
                    key,
                    rendered_file.size_bytes,
                    rendered_file.content_sha256,
                )
                if attempt_check is not None:
                    await attempt_check()
            return keys
        except BaseException:
            await self._remove_many(keys)
            raise


__all__ = ["GenerationWorker"]
