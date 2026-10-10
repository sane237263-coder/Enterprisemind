"""Rebuild previews from committed extraction; never replace sources, anchors or indexes."""

import asyncio
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.ingestion.media import register_artifact
from enterprise_mind.ingestion.models import (
    ContentElement,
    Document,
    DocumentArtifact,
    ProcessingTask,
    ProcessingUnit,
    Revision,
    SourceAnchor,
    SourceUnit,
    StorageObject,
)
from enterprise_mind.ingestion.parsing import (
    ParsedArtifact,
    ParsedDocument,
    ParsedElement,
    ParsedSource,
)
from enterprise_mind.ingestion.process import run_parser
from enterprise_mind.ingestion.service import processing_event, utc

if TYPE_CHECKING:
    from enterprise_mind.ingestion.worker import IngestionWorker, Lease


async def request_rebuild(
    session: AsyncSession, document: Document, revision_id: int
) -> dict[str, Any]:
    revision = await session.scalar(
        select(Revision).where(Revision.id == revision_id).with_for_update()
    )
    if (
        revision is None
        or revision.document_id != document.id
        or revision.retired_at is not None
        or revision.id != document.active_revision_id
    ):
        raise APIError(ErrorCode.SOURCE_UNAVAILABLE)
    busy = await session.scalar(
        select(ProcessingTask.id)
        .join(Revision)
        .where(
            Revision.document_id == document.id,
            ProcessingTask.status.in_(("pending", "processing")),
        )
        .limit(1)
    )
    if busy:
        raise APIError(ErrorCode.DOCUMENT_PROCESSING_IN_PROGRESS)
    task = ProcessingTask(
        revision_id=revision.id,
        task_type="preview_rebuild",
        current_stage="preview",
        stage_current=0,
        stage_total=1,
    )
    session.add(task)
    await session.flush()
    unit = await session.scalar(
        select(ProcessingUnit)
        .where(
            ProcessingUnit.revision_id == revision.id,
            ProcessingUnit.stage == "preview",
            ProcessingUnit.unit_key == "rebuild",
        )
        .with_for_update()
    )
    if unit is None:
        unit = ProcessingUnit(revision_id=revision.id, stage="preview", unit_key="rebuild")
        session.add(unit)
    unit.latest_task_id, unit.status = task.id, "pending"
    unit.current, unit.total = 0, 1
    unit.error_code = unit.error_message = None
    unit.lease_owner = unit.lease_expires_at = None
    unit.started_at = unit.finished_at = None
    session.add(processing_event(document.id, revision.id, task.id))
    await session.flush()
    return {
        "id": str(document.id),
        "processing_task_id": str(task.id),
        "revision_id": str(revision.id),
        "status": "pending",
    }


async def rebuild_status(session: AsyncSession, revision_id: int) -> dict[str, Any] | None:
    task = await session.scalar(
        select(ProcessingTask)
        .where(
            ProcessingTask.revision_id == revision_id, ProcessingTask.task_type == "preview_rebuild"
        )
        .order_by(ProcessingTask.id.desc())
        .limit(1)
    )
    if task is None:
        return None
    return {
        "id": str(task.id),
        "status": task.status,
        "error_code": task.error_code,
        "error_message": task.error_message,
        "updated_at": utc(task.updated_at),
    }


async def prepare_preview(worker: "IngestionWorker", lease: "Lease") -> ParsedDocument:
    if lease.extension == "pdf":
        # The original PDF is already validated and needs no new copy in object storage.
        return ParsedDocument(sources=[])
    config, storage = worker.config, worker.storage
    async with worker.sessions() as session:
        sources = list(
            (
                await session.scalars(
                    select(SourceUnit)
                    .where(SourceUnit.revision_id == lease.revision_id)
                    .order_by(SourceUnit.id)
                    .limit(config.max_source_units + 1)
                )
            ).all()
        )
        if not sources or len(sources) > config.max_source_units:
            raise ValueError("preview source limit")
        elements = list(
            (
                await session.scalars(
                    select(ContentElement)
                    .join(SourceUnit)
                    .where(
                        SourceUnit.revision_id == lease.revision_id,
                        ContentElement.status == "success",
                    )
                    .order_by(ContentElement.source_unit_id, ContentElement.ordinal)
                    .limit(config.max_elements + 1)
                )
            ).all()
        )
        if (
            len(elements) > config.max_elements
            or sum(len(e.content_text or "") for e in elements) > config.max_text_chars
        ):
            raise ValueError("preview content limit")
        anchors = list(
            (
                await session.scalars(
                    select(SourceAnchor)
                    .join(ContentElement, ContentElement.id == SourceAnchor.element_id)
                    .where(
                        SourceAnchor.revision_id == lease.revision_id,
                        ContentElement.status == "success",
                    )
                    .distinct(SourceAnchor.element_id)
                    .order_by(SourceAnchor.element_id, SourceAnchor.id)
                    .limit(config.max_elements)
                )
            ).all()
        )
        images = list(
            (
                await session.execute(
                    select(DocumentArtifact.source_unit_id, StorageObject)
                    .join(StorageObject, StorageObject.id == DocumentArtifact.storage_object_id)
                    .where(
                        DocumentArtifact.revision_id == lease.revision_id,
                        DocumentArtifact.artifact_type == "page_image",
                        DocumentArtifact.status == "ready",
                    )
                )
            ).all()
        )
    anchor_by_element = {a.element_id: a for a in reversed(anchors)}
    result = ParsedDocument(sources=[])
    indexes = {source.id: index for index, source in enumerate(sources)}
    elements_by_source: dict[int, list[ContentElement]] = {}
    for element in elements:
        elements_by_source.setdefault(element.source_unit_id, []).append(element)
    for source in sources:
        parsed = ParsedSource(
            kind=source.unit_type,
            ordinal=source.ordinal,
            label=source.label,
            text="",
            width=float(source.width) if source.width else None,
            height=float(source.height) if source.height else None,
            metadata=source.metadata_ or {},
        )
        for element in elements_by_source.get(source.id, []):
            anchor = anchor_by_element.get(element.id)
            parsed.elements.append(
                ParsedElement(
                    kind=element.element_type,
                    text=element.content_text or "",
                    payload={
                        **(element.structured_payload or {}),
                        "preview_ordinal": element.ordinal,
                    },
                    locator_type=anchor.locator_type if anchor else "text",
                    locator=anchor.locator if anchor else {},
                    source_text=anchor.source_text or "" if anchor else "",
                )
            )
        result.sources.append(parsed)
    with TemporaryDirectory(prefix="em-rebuild-") as directory:
        root = Path(directory)
        artifacts = root / "artifacts"
        artifacts.mkdir()
        total = 0
        for source_id, obj in images:
            total += obj.size_bytes
            if (
                obj.status != "available"
                or not obj.content_sha256
                or obj.bucket_name != storage.bucket
                or total > config.max_artifact_bytes
            ):
                raise ValueError("preview image unavailable")
            index = indexes[source_id]
            name = f"image-{index}.png"
            await asyncio.to_thread(
                storage.download,
                obj.object_key,
                artifacts / name,
                obj.size_bytes,
                obj.content_sha256,
            )
            result.artifacts.append(
                ParsedArtifact(
                    file_name=name,
                    artifact_type="page_image",
                    content_type="image/png",
                    source_index=index,
                )
            )
        snapshot = root / "snapshot.json"
        await asyncio.to_thread(snapshot.write_text, result.model_dump_json(), encoding="utf-8")
        rendered = await run_parser(snapshot, "preview_snapshot", config, root)
        artifact = next(a for a in rendered.artifacts if a.artifact_type == "preview_html")
        path = artifacts / artifact.file_name
        content = await asyncio.to_thread(path.read_bytes)
        artifact.size, artifact.digest = len(content), hashlib.sha256(content).hexdigest()
        artifact.object_key = storage.new_key()
        with path.open("rb") as handle:
            await asyncio.to_thread(
                storage.upload, artifact.object_key, handle, artifact.content_type
            )
        await asyncio.to_thread(storage.verify, artifact.object_key, artifact.size, artifact.digest)
        # Only replace the document preview; source images and structured content stay untouched.
        return ParsedDocument(sources=[], artifacts=[artifact])


async def persist_preview(
    session: AsyncSession, worker: "IngestionWorker", lease: "Lease", parsed: ParsedDocument
) -> None:
    if lease.extension == "pdf":
        revision = await session.get(Revision, lease.revision_id)
        assert revision is not None
        artifact = await session.scalar(
            select(DocumentArtifact).where(
                DocumentArtifact.revision_id == revision.id,
                DocumentArtifact.artifact_type == "preview_pdf",
                DocumentArtifact.source_unit_id.is_(None),
            )
        )
        if artifact is None:
            session.add(
                DocumentArtifact(
                    revision_id=revision.id,
                    artifact_type="preview_pdf",
                    storage_object_id=revision.original_object_id,
                    status="ready",
                )
            )
        else:
            artifact.status = "ready"
    else:
        for artifact_data in parsed.artifacts:
            await register_artifact(session, worker.storage, lease.revision_id, None, artifact_data)
