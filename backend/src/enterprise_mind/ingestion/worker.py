import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from enterprise_mind.database.export_runtime import ExportRuntimePlanStore
from enterprise_mind.database.mcp_client import DatabaseMCPClient
from enterprise_mind.generation.worker import GenerationWorker
from enterprise_mind.identity.security import parse_id
from enterprise_mind.infrastructure.settings import Phase9Config, WorkerConfig
from enterprise_mind.ingestion.media import MediaPrepared, MediaService, register_artifact
from enterprise_mind.ingestion.models import (
    ContentElement,
    Document,
    DocumentArtifact,
    OutboxEvent,
    ProcessingTask,
    ProcessingUnit,
    Revision,
    SourceAnchor,
    SourceUnit,
    StorageObject,
)
from enterprise_mind.ingestion.office import LEGACY_FORMATS, OfficeConverter
from enterprise_mind.ingestion.parsing import ParsedDocument
from enterprise_mind.ingestion.process import run_parser
from enterprise_mind.ingestion.queue import TaskQueue
from enterprise_mind.ingestion.storage import ObjectStorage
from enterprise_mind.knowledge.models import DeletionBatch, KnowledgeBase
from enterprise_mind.rag.indexing import IndexingService, IndexResult

SUPPORTED_STAGES = ("parse", "table", "code")
RECOVERABLE_STAGES = (
    *SUPPORTED_STAGES,
    "ocr",
    "vision",
    "chunk",
    "embedding",
    "activate",
    "preview",
)
STAGE_ORDER = (
    "parse",
    "ocr",
    "vision",
    "table",
    "code",
    "chunk",
    "embedding",
    "keyword_index",
    "preview",
    "activate",
)


@dataclass(frozen=True)
class Lease:
    document_id: int
    revision_id: int
    task_id: int
    unit_id: int
    owner: str
    stage: str
    unit_key: str
    object_key: str
    extension: str
    size: int
    digest: str


async def lock_task(
    session: AsyncSession, task_id: int
) -> tuple[Document, Revision, ProcessingTask] | None:
    ids = (
        await session.execute(
            select(Revision.document_id, Revision.id)
            .join(ProcessingTask, ProcessingTask.revision_id == Revision.id)
            .where(ProcessingTask.id == task_id)
        )
    ).one_or_none()
    if ids is None:
        return None
    document = await session.scalar(
        select(Document)
        .where(Document.id == ids[0])
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    revision = await session.scalar(
        select(Revision)
        .where(Revision.id == ids[1])
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    task = await session.scalar(
        select(ProcessingTask)
        .where(ProcessingTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if document is None or revision is None or task is None:
        return None
    return document, revision, task


async def not_purging(session: AsyncSession, document: Document) -> bool:
    base = await session.get(KnowledgeBase, document.knowledge_base_id)
    if base is None:
        return False
    batches = [
        value for value in (document.deletion_batch_id, base.deletion_batch_id) if value is not None
    ]
    return (
        not batches
        or await session.scalar(
            select(DeletionBatch.id)
            .where(DeletionBatch.id.in_(batches), DeletionBatch.status.in_(["purging", "failed"]))
            .limit(1)
        )
        is None
    )


async def update_progress(
    session: AsyncSession, document: Document, revision: Revision, task: ProcessingTask
) -> None:
    units = list(
        (
            await session.scalars(
                select(ProcessingUnit).where(ProcessingUnit.latest_task_id == task.id)
            )
        ).all()
    )
    now = datetime.now(UTC)
    failed = [unit for unit in units if unit.status == "failed"]
    if task.task_type == "preview_rebuild":
        task.updated_at = now
        task.current_stage, task.stage_total = "preview", 1
        complete = all(unit.status in {"success", "failed", "skipped"} for unit in units)
        task.stage_current = int(complete)
        task.status = ("failed" if failed else "success") if complete else "processing"
        task.finished_at = now if complete else None
        task.error_code = failed[0].error_code if failed else None
        task.error_message = failed[0].error_message if failed else None
        return
    task.updated_at = document.updated_at = revision.updated_at = now
    if any(unit.stage == "parse" and unit.status == "failed" for unit in units):
        task.status = revision.processing_status = document.processing_status = "failed"
        task.finished_at = now
        task.error_code, task.error_message = failed[0].error_code, failed[0].error_message
        revision.failure_summary = {
            "failed_unit_count": len(failed),
            "stages": sorted({unit.stage for unit in failed}),
        }
    else:
        # IndexingService alone evaluates activation after all required units terminate.
        task.status = "processing"
    for stage in STAGE_ORDER:
        stage_units = [unit for unit in units if unit.stage == stage]
        if stage_units and any(unit.status in {"pending", "processing"} for unit in stage_units):
            task.current_stage = stage
            task.stage_current = sum(
                unit.status in {"success", "failed", "skipped"} for unit in stage_units
            )
            task.stage_total = len(stage_units)
            break
    else:
        task.current_stage = "parse"
        task.stage_current = task.stage_total = 1


class IngestionWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        queue: TaskQueue,
        config: WorkerConfig,
        indexing: IndexingService | None = None,
        media: MediaService | None = None,
        converter: OfficeConverter | None = None,
        phase9: Phase9Config | None = None,
        database: DatabaseMCPClient | None = None,
        export_plans: ExportRuntimePlanStore | None = None,
    ) -> None:
        self.sessions, self.storage, self.queue, self.config = sessions, storage, queue, config
        self.consumer = "worker-" + uuid4().hex
        self.indexing = indexing
        self.media = media or MediaService(sessions, storage, config)
        self.converter = converter
        self.media.converter = converter
        self.generation = GenerationWorker(
            sessions,
            storage,
            phase9 or Phase9Config(),
            worker_config=config,
            database=database,
            export_plans=export_plans,
        )
        self.supported_stages = (
            *SUPPORTED_STAGES,
            "preview",
            *self.media.stages,
            *(("chunk", "embedding", "activate") if indexing else ()),
        )

    async def claim(self, task_id: int) -> Lease | None:
        async with self.sessions() as session, session.begin():
            state = await lock_task(session, task_id)
            if state is None:
                return None
            document, revision, task = state
            if (
                task.status not in {"pending", "processing"}
                or revision.retired_at is not None
                or not await not_purging(session, document)
            ):
                return None
            dependency = aliased(ProcessingUnit)
            unit = await session.scalar(
                select(ProcessingUnit)
                .where(
                    ProcessingUnit.latest_task_id == task.id,
                    ProcessingUnit.stage.in_(self.supported_stages),
                    ProcessingUnit.status == "pending",
                    or_(
                        ProcessingUnit.stage != "chunk",
                        ~select(dependency.id)
                        .where(
                            dependency.revision_id == ProcessingUnit.revision_id,
                            dependency.source_unit_id == ProcessingUnit.source_unit_id,
                            dependency.stage.in_(("ocr", "vision", "table", "code")),
                            dependency.status.in_(("pending", "processing")),
                        )
                        .exists(),
                    ),
                )
                .order_by(ProcessingUnit.id)
                .with_for_update()
                .limit(1)
            )
            if unit is None:
                return None
            if unit.stage == "activate":
                base = await session.get(KnowledgeBase, document.knowledge_base_id)
                if document.deleted_at is not None or base is None or base.deleted_at is not None:
                    return None
            original = await session.get(StorageObject, revision.original_object_id)
            if original is None or original.status != "available":
                return None
            now = datetime.now(UTC)
            owner = uuid4().hex
            unit.status, unit.lease_owner = "processing", owner
            unit.lease_expires_at = now + timedelta(seconds=self.config.lease_seconds)
            unit.attempt_count += 1
            unit.started_at, unit.finished_at, unit.updated_at = now, None, now
            unit.current, unit.total = 0, 1
            if task.started_at is None:
                task.started_at = now
            task.status = "processing"
            if (
                task.task_type != "preview_rebuild"
                and revision.processing_status != "partial_success"
            ):
                revision.processing_status = "processing"
                if revision.revision_no == document.latest_revision_no:
                    document.processing_status = "processing"
            await session.flush()
            await update_progress(session, document, revision, task)
            return Lease(
                document.id,
                revision.id,
                task.id,
                unit.id,
                owner,
                unit.stage,
                unit.unit_key,
                original.object_key,
                revision.file_extension,
                revision.size_bytes,
                revision.content_sha256,
            )

    async def renew(self, lease: Lease) -> bool:
        async with self.sessions() as session, session.begin():
            state = await lock_task(session, lease.task_id)
            if state is None or not await not_purging(session, state[0]):
                return False
            unit = await session.scalar(
                select(ProcessingUnit).where(ProcessingUnit.id == lease.unit_id).with_for_update()
            )
            if not self.valid(unit, lease):
                return False
            assert unit is not None
            unit.lease_expires_at = datetime.now(UTC) + timedelta(seconds=self.config.lease_seconds)
            return True

    @staticmethod
    def valid(unit: ProcessingUnit | None, lease: Lease) -> bool:
        return bool(
            unit is not None
            and unit.status == "processing"
            and unit.latest_task_id == lease.task_id
            and unit.lease_owner == lease.owner
            and unit.lease_expires_at is not None
            and unit.lease_expires_at > datetime.now(UTC)
        )

    async def persist(
        self,
        lease: Lease,
        parsed: ParsedDocument | None,
        error: str | None = None,
        indexed: IndexResult | None = None,
        media: MediaPrepared | None = None,
    ) -> bool:
        async with self.sessions() as session, session.begin():
            state = await lock_task(session, lease.task_id)
            if state is None or not await not_purging(session, state[0]):
                return False
            document, revision, task = state
            unit = await session.scalar(
                select(ProcessingUnit).where(ProcessingUnit.id == lease.unit_id).with_for_update()
            )
            if not self.valid(unit, lease):
                return False
            assert unit is not None
            if task.task_type == "preview_rebuild":
                base = await session.get(KnowledgeBase, document.knowledge_base_id)
                if (
                    document.deleted_at is not None
                    or base is None
                    or base.deleted_at is not None
                    or revision.retired_at is not None
                    or document.active_revision_id != revision.id
                ):
                    parsed, error = None, "PREVIEW_SOURCE_UNAVAILABLE"
                if parsed is not None:
                    from enterprise_mind.ingestion.preview_rebuild import persist_preview

                    await persist_preview(session, self, lease, parsed)
            elif parsed is not None:
                await self.save_parsed(session, lease, parsed)
            if indexed is not None:
                assert self.indexing is not None
                await self.indexing.persist(session, lease, document, indexed)
            if media is not None:
                await self.media.persist(session, lease, media)
            if error and lease.stage in {"table", "code"}:
                element = await session.get(ContentElement, parse_id(lease.unit_key.split(":")[1]))
                if element is not None:
                    element.status = "failed"
            # A large output can take time to insert. Do not commit after the original lease.
            if not self.valid(unit, lease):
                await session.rollback()
                return False
            now = datetime.now(UTC)
            unit.status = "failed" if error else "success"
            unit.error_code = error
            unit.error_message = {
                "PARSE_FAILED": "无法完成基础解析，请检查文件格式或内容。",
                "PREVIEW_REBUILD_FAILED": "预览重建失败，已有内容仍可使用，请稍后重试。",
                "PREVIEW_SOURCE_UNAVAILABLE": "文档状态已变化，未替换预览。",
                "SOURCE_OBJECT_UNAVAILABLE": "原文件暂时无法读取或未通过完整性校验。",
                "TABLE_EXTRACTION_FAILED": "表格结构校验失败。",
                "CODE_EXTRACTION_FAILED": "代码结构校验失败。",
                "CHUNK_SOURCE_INVALID": "分块来源未通过一致性校验。",
                "CHUNK_STRUCTURE_TOO_LARGE": "完整结构超出模型输入限制，无法安全拆分。",
                "CHUNK_NO_CONTENT": "没有可索引的文本来源。",
                "EMBEDDING_FAILED": "向量生成失败，请检查模型配置或服务后重试。",
                "OFFICE_CONVERTER_NOT_CONFIGURED": "Office 转换或图表渲染需要配置转换服务后重试。",
                "OFFICE_CONVERSION_FAILED": "Office 转换失败，请检查格式或转换服务后重试。",
                "IMAGE_DECODE_FAILED": "此图片无法安全解码，其他可用内容继续处理。",
                "MEDIA_SOURCE_UNAVAILABLE": "图片来源暂时不可用。",
                "MEDIA_PROVIDER_UNAVAILABLE": "OCR 或 Vision 服务暂时不可用。",
                "MEDIA_PROVIDER_RATE_LIMITED": "OCR 或 Vision 请求触发限流，请稍后重试。",
                "MEDIA_PROVIDER_BUSY": "OCR 或 Vision 模型服务繁忙，请稍后重试。",
                "MEDIA_PROVIDER_QUOTA_EXHAUSTED": "OCR 或 Vision 额度或余额不足，请检查配置。",
                "MEDIA_PROVIDER_REJECTED": "OCR 或 Vision 服务拒绝请求。",
                "MEDIA_RESPONSE_INVALID": "OCR 或 Vision 结果不符合格式或大小限制。",
                "OCR_RESPONSE_INVALID": "OCR 文本或坐标校验失败。",
                "VISION_RESPONSE_INVALID": "Vision 结果不完整或不符合结构。",
            }.get(error or "")
            unit.lease_owner = unit.lease_expires_at = None
            unit.finished_at = unit.updated_at = now
            unit.current = unit.total = 1
            await session.flush()
            await update_progress(session, document, revision, task)
            if self.indexing is not None and task.task_type != "preview_rebuild":
                await self.indexing.finalize(session, document, revision, task)
            return True

    async def save_parsed(
        self, session: AsyncSession, lease: Lease, parsed: ParsedDocument
    ) -> None:
        # The lease and one transaction protect replacement of uncommitted/failed parse output.
        await session.execute(delete(SourceUnit).where(SourceUnit.revision_id == lease.revision_id))
        source_ids = []
        for source in parsed.sources:
            row = SourceUnit(
                revision_id=lease.revision_id,
                unit_type=source.kind,
                ordinal=source.ordinal,
                label=source.label,
                width=Decimal(str(source.width)) if source.width else None,
                height=Decimal(str(source.height)) if source.height else None,
                extraction_status="pending" if source.metadata.get("needs_ocr") else "success",
                extracted_text=source.text,
                metadata_=source.metadata,
            )
            session.add(row)
            await session.flush()
            source_ids.append(row.id)
            elements: list[int] = []
            for ordinal, element in enumerate(source.elements):
                content = ContentElement(
                    source_unit_id=row.id,
                    parent_element_id=elements[element.parent]
                    if element.parent is not None
                    else None,
                    element_type=element.kind,
                    ordinal=ordinal,
                    content_text=element.text,
                    structured_payload=element.payload,
                )
                session.add(content)
                await session.flush()
                elements.append(content.id)
                session.add(
                    SourceAnchor(
                        revision_id=lease.revision_id,
                        source_unit_id=row.id,
                        element_id=content.id,
                        locator_type=element.locator_type,
                        locator=element.locator,
                        source_text=element.source_text or None,
                        source_text_sha256=hashlib.sha256(element.source_text.encode()).hexdigest()
                        if element.source_text
                        else None,
                    )
                )
                if element.kind in {"table", "code"}:
                    session.add(
                        ProcessingUnit(
                            revision_id=lease.revision_id,
                            latest_task_id=lease.task_id,
                            stage=element.kind,
                            unit_key=f"element:{content.id}",
                            source_unit_id=row.id,
                        )
                    )
            for stage in ("ocr", "vision", "chunk"):
                if stage == "chunk" or source.metadata.get("needs_" + stage):
                    session.add(
                        ProcessingUnit(
                            revision_id=lease.revision_id,
                            latest_task_id=lease.task_id,
                            stage=stage,
                            unit_key=f"{source.kind}:{row.id}",
                            source_unit_id=row.id,
                        )
                    )

        for artifact in parsed.artifacts:
            await register_artifact(
                session,
                self.storage,
                lease.revision_id,
                source_ids[artifact.source_index] if artifact.source_index is not None else None,
                artifact,
            )
        if lease.extension == "pdf":
            revision = await session.get(Revision, lease.revision_id)
            assert revision is not None
            session.add(
                DocumentArtifact(
                    revision_id=lease.revision_id,
                    artifact_type="preview_pdf",
                    storage_object_id=revision.original_object_id,
                    status="ready",
                )
            )
        if parsed.artifacts or lease.extension == "pdf":
            session.add(
                ProcessingUnit(
                    revision_id=lease.revision_id,
                    latest_task_id=lease.task_id,
                    stage="preview",
                    unit_key="document",
                    status="success",
                    current=1,
                    total=1,
                    finished_at=datetime.now(UTC),
                )
            )

    async def parse(self, lease: Lease) -> ParsedDocument:
        with TemporaryDirectory(prefix="enterprise-mind-parse-") as directory:
            source = Path(directory) / ("source." + lease.extension)
            await asyncio.to_thread(
                self.storage.download, lease.object_key, source, lease.size, lease.digest
            )
            extension = lease.extension
            if extension in LEGACY_FORMATS:
                if self.converter is None:
                    raise ValueError("OFFICE_CONVERTER_NOT_CONFIGURED")
                converted = Path(directory) / ("converted." + LEGACY_FORMATS[extension])
                extension = await self.converter.convert(source, extension, converted)
                source = converted
            artifact_dir = Path(directory) / "artifacts"
            parsed = await run_parser(source, extension, self.config, Path(directory))
            for artifact in parsed.artifacts:
                path = artifact_dir / artifact.file_name
                artifact.size = path.stat().st_size
                data = await asyncio.to_thread(path.read_bytes)
                digest = hashlib.sha256(data).hexdigest()
                artifact.digest = digest
                artifact.object_key = self.storage.new_key()
                with path.open("rb") as handle:
                    await asyncio.to_thread(
                        self.storage.upload, artifact.object_key, handle, artifact.content_type
                    )
                await asyncio.to_thread(
                    self.storage.verify, artifact.object_key, artifact.size, digest
                )
            return parsed

    async def execute(self, lease: Lease) -> bool:
        if lease.stage in {"ocr", "vision"}:
            job_media = asyncio.create_task(self.media.prepare(lease))
            try:
                while True:
                    done_media, _ = await asyncio.wait(
                        {job_media}, timeout=self.config.lease_renew_interval_seconds
                    )
                    if done_media:
                        result_media = await job_media
                        return await self.persist(
                            lease, None, result_media.error, media=result_media
                        )
                    if not await self.renew(lease):
                        return False
            finally:
                if not job_media.done():
                    job_media.cancel()
                    await asyncio.gather(job_media, return_exceptions=True)
        if lease.stage in {"chunk", "embedding", "activate"}:
            assert self.indexing is not None
            job_index = asyncio.create_task(self.indexing.prepare(lease))
            try:
                while True:
                    done_index, _ = await asyncio.wait(
                        {job_index}, timeout=self.config.lease_renew_interval_seconds
                    )
                    if done_index:
                        indexed = await job_index
                        return await self.persist(lease, None, indexed.error, indexed)
                    if not await self.renew(lease):
                        return False
            finally:
                if not job_index.done():
                    job_index.cancel()
                    await asyncio.gather(job_index, return_exceptions=True)

        async def work() -> tuple[ParsedDocument | None, str | None]:
            if lease.stage == "preview":
                from enterprise_mind.ingestion.preview_rebuild import prepare_preview

                try:
                    return await prepare_preview(self, lease), None
                except Exception:
                    return None, "PREVIEW_REBUILD_FAILED"
            if lease.stage == "parse":
                try:
                    return await self.parse(lease), None
                except (ValueError, TimeoutError) as error:
                    return None, str(error) if str(
                        error
                    ) == "OFFICE_CONVERTER_NOT_CONFIGURED" else "PARSE_FAILED"
                except Exception as error:
                    from enterprise_mind.rag.providers import ProviderFailure

                    return None, str(error) if isinstance(
                        error, ProviderFailure
                    ) else "SOURCE_OBJECT_UNAVAILABLE"
            async with self.sessions() as session, session.begin():
                element = await session.get(ContentElement, parse_id(lease.unit_key.split(":")[1]))
                payload = element.structured_payload if element else None
                required = (
                    {"headers", "rows", "source_range", "merged_ranges"}
                    if lease.stage == "table"
                    else {"language"}
                )
                if payload is None or not required <= payload.keys():
                    return (
                        None,
                        "TABLE_EXTRACTION_FAILED"
                        if lease.stage == "table"
                        else "CODE_EXTRACTION_FAILED",
                    )
            return None, None

        job = asyncio.create_task(work())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {job}, timeout=self.config.lease_renew_interval_seconds
                )
                if done:
                    parsed, error = await job
                    return await self.persist(lease, parsed, error)
                if not await self.renew(lease):
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
                    return False
        finally:
            if not job.done():
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)

    async def handle_event(self, event_id: int) -> bool:
        async with self.sessions() as session, session.begin():
            event = await session.get(OutboxEvent, event_id)
            if event is None:
                return True
            generation_requested = event.event_type in {
                "generation.requested",
                "generation.export.requested",
            }
            processing = event.event_type == "document.processing.requested"
            task_id = parse_id(event.payload["task_id"]) if processing else None
        if generation_requested:
            return await self.generation.handle_event(event_id)
        if task_id is None:
            from enterprise_mind.ingestion.cleanup import CleanupWorker

            return await CleanupWorker(self.sessions, self.storage).handle(event_id)
        while (lease := await self.claim(task_id)) is not None:
            if not await self.execute(lease):
                return False
        async with self.sessions() as session, session.begin():
            remaining = await session.scalar(
                select(ProcessingUnit.id)
                .where(
                    ProcessingUnit.latest_task_id == task_id,
                    ProcessingUnit.stage.in_(self.supported_stages),
                    ProcessingUnit.status.in_(["pending", "processing"]),
                )
                .limit(1)
            )
            return remaining is None

    async def run_once(self) -> bool:
        message = await self.queue.receive(self.consumer, self.config.lease_seconds)
        if message is None:
            return False
        message_id, data = message
        try:
            event_id = parse_id(data["event_id"])
        except (ValueError, KeyError, TypeError):
            await self.queue.ack(message_id)
            return True
        if await self.handle_event(event_id):
            await self.queue.ack(message_id)
        return True
