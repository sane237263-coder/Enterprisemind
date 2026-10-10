"""OCR/Vision preparation outside transactions and lease-guarded persistence."""

import asyncio
import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_mind.infrastructure.settings import WorkerConfig
from enterprise_mind.ingestion.media_providers import OCRProvider, VisionProvider
from enterprise_mind.ingestion.models import (
    ContentElement,
    DocumentArtifact,
    ProcessingUnit,
    SourceAnchor,
    SourceUnit,
    StorageObject,
)
from enterprise_mind.ingestion.office import LEGACY_FORMATS, OfficeConverter
from enterprise_mind.ingestion.parsing import ParsedArtifact, ParsedElement
from enterprise_mind.ingestion.process import run_parser
from enterprise_mind.ingestion.storage import ObjectStorage
from enterprise_mind.rag.providers import ProviderFailure

if TYPE_CHECKING:
    from enterprise_mind.ingestion.worker import Lease


@dataclass
class MediaPrepared:
    elements: list[ParsedElement] = field(default_factory=list)
    artifact: ParsedArtifact | None = None
    error: str | None = None
    image_artifact: ParsedArtifact | None = None
    image_metadata: dict[str, Any] = field(default_factory=dict)


async def register_artifact(
    session: AsyncSession,
    storage: ObjectStorage,
    revision_id: int,
    source_id: int | None,
    artifact: ParsedArtifact,
) -> None:
    from enterprise_mind.ingestion.cleanup import queue_storage_delete

    previous = await session.scalar(
        select(DocumentArtifact).where(
            DocumentArtifact.revision_id == revision_id,
            DocumentArtifact.source_unit_id == source_id,
            DocumentArtifact.artifact_type == artifact.artifact_type,
        )
    )
    if previous is not None:
        old = await session.get(StorageObject, previous.storage_object_id)
        await session.delete(previous)
        await session.flush()
        if old is not None:
            await queue_storage_delete(session, old)
    if not artifact.object_key or not artifact.digest:
        raise ValueError("artifact has no verified object")
    obj = StorageObject(
        bucket_name=storage.bucket,
        object_key=artifact.object_key,
        object_type="preview" if artifact.artifact_type.startswith("preview") else "derived",
        size_bytes=artifact.size,
        content_type=artifact.content_type,
        content_sha256=artifact.digest,
        status="available",
    )
    session.add(obj)
    await session.flush()
    session.add(
        DocumentArtifact(
            revision_id=revision_id,
            source_unit_id=source_id,
            artifact_type=artifact.artifact_type,
            storage_object_id=obj.id,
            status="ready",
            metadata_=artifact.metadata,
        )
    )


class MediaService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        config: WorkerConfig,
        ocr: OCRProvider | None = None,
        vision: VisionProvider | None = None,
        converter: OfficeConverter | None = None,
    ) -> None:
        self.sessions, self.storage, self.config = sessions, storage, config
        self.ocr, self.vision = ocr, vision
        self.converter = converter

    @property
    def stages(self) -> tuple[str, ...]:
        return tuple(
            name for name, provider in (("ocr", self.ocr), ("vision", self.vision)) if provider
        )

    async def prepare(self, lease: "Lease") -> MediaPrepared:
        image_artifact = None
        image_metadata: dict[str, Any] = {}
        try:
            async with self.sessions() as session:
                unit = await session.get(ProcessingUnit, lease.unit_id)
                source = await session.get(SourceUnit, unit.source_unit_id) if unit else None
                if source is None or source.revision_id != lease.revision_id:
                    return MediaPrepared(error="MEDIA_SOURCE_INVALID")
                metadata = source.metadata_ or {}
                if metadata.get("visual_error"):
                    return MediaPrepared(error="IMAGE_DECODE_FAILED")
                obj = await session.scalar(
                    select(StorageObject)
                    .join(DocumentArtifact, DocumentArtifact.storage_object_id == StorageObject.id)
                    .where(
                        DocumentArtifact.revision_id == lease.revision_id,
                        DocumentArtifact.source_unit_id == source.id,
                        DocumentArtifact.artifact_type == "page_image",
                        DocumentArtifact.status == "ready",
                        StorageObject.status == "available",
                    )
                )
                needs_render = metadata.get("needs_render")
                if obj is None and lease.extension != "pdf" and not needs_render:
                    return MediaPrepared(error="MEDIA_SOURCE_UNAVAILABLE")
            with TemporaryDirectory(prefix="em-media-") as directory:
                path = Path(directory) / "image.png"
                if obj is None:
                    # PDFs parsed before this migration have no registered page image yet.
                    original = Path(directory) / ("source." + lease.extension)
                    await asyncio.to_thread(
                        self.storage.download, lease.object_key, original, lease.size, lease.digest
                    )
                    expected_pages = None
                    if needs_render:
                        if self.converter is None:
                            return MediaPrepared(error="OFFICE_CONVERTER_NOT_CONFIGURED")
                        rendered_pdf = Path(directory) / "visual.pdf"
                        if "render_unit" in metadata:
                            extension = lease.extension
                            if extension in LEGACY_FORMATS:
                                converted = Path(directory) / (
                                    "converted." + LEGACY_FORMATS[extension]
                                )
                                extension = await self.converter.convert(
                                    original, extension, converted
                                )
                                original = converted
                            await self.converter.render(
                                original, extension, rendered_pdf, unit=int(metadata["render_unit"])
                            )
                            expected_pages = 1
                        else:
                            await self.converter.render(original, lease.extension, rendered_pdf)
                            expected_pages = int(metadata["slide_count"])
                        original = rendered_pdf
                    rendered = await run_parser(
                        original,
                        "pdf",
                        self.config,
                        Path(directory),
                        page=0 if "render_unit" in metadata else source.ordinal,
                        expected_pages=expected_pages,
                    )
                    image_artifact = rendered.artifacts[0]
                    path = Path(directory) / "artifacts" / image_artifact.file_name
                    image_metadata = {
                        key: value
                        for key, value in rendered.sources[0].metadata.items()
                        if key in {"image_width", "image_height"}
                    }
                    metadata = {**metadata, **image_metadata}
                else:
                    if not obj.content_sha256 or obj.size_bytes > self.config.max_artifact_bytes:
                        return MediaPrepared(error="MEDIA_SOURCE_UNAVAILABLE")
                    await asyncio.to_thread(
                        self.storage.download,
                        obj.object_key,
                        path,
                        obj.size_bytes,
                        obj.content_sha256,
                    )
                data = await asyncio.to_thread(path.read_bytes)
            if image_artifact is not None:
                image_artifact.object_key = self.storage.new_key()
                image_artifact.size = len(data)
                image_artifact.digest = hashlib.sha256(data).hexdigest()
                await asyncio.to_thread(
                    self.storage.upload, image_artifact.object_key, io.BytesIO(data), "image/png"
                )
                await asyncio.to_thread(
                    self.storage.verify,
                    image_artifact.object_key,
                    image_artifact.size,
                    image_artifact.digest,
                )
            width, height = int(metadata["image_width"]), int(metadata["image_height"])
            locator_type = metadata.get(
                "locator_type", "pdf" if source.unit_type == "page" else "image"
            )
            origin: dict[str, Any] = dict(metadata.get("origin_locator", {}))
            if locator_type == "pdf":
                origin["page"] = source.ordinal + 1
            full_box = {
                "x": 0.0,
                "y": 0.0,
                "width": float(source.width or width),
                "height": float(source.height or height),
            }
            target = origin.get("boxes", [full_box])[0]
            elements = []
            if lease.stage == "ocr":
                from enterprise_mind.ingestion.rich_parsing import table_element

                assert self.ocr is not None
                ocr = await self.ocr.recognize(data, width, height)
                raw = ocr.model_dump_json().encode()
                model_key = self.ocr.model_key
                for table in ocr.tables:
                    box = {
                        "x": target["x"] + table.box.x * target["width"] / width,
                        "y": target["y"] + table.box.y * target["height"] / height,
                        "width": table.box.width * target["width"] / width,
                        "height": table.box.height * target["height"] / height,
                    }
                    locator = {
                        **origin,
                        **(
                            {"boxes": [box]}
                            if needs_render or locator_type in {"pdf", "image", "pptx"}
                            else {}
                        ),
                    }
                    elements.append(
                        table_element(
                            table.rows,
                            locator_type,
                            locator,
                            table.merged_ranges,
                            text_origin="ocr",
                            model_key=model_key,
                        )
                    )
                for block in ocr.blocks:
                    if (
                        block.box.x + block.box.width > width
                        or block.box.y + block.box.height > height
                    ):
                        raise ProviderFailure("OCR_RESPONSE_INVALID")
                    box = {
                        "x": target["x"] + block.box.x * target["width"] / width,
                        "y": target["y"] + block.box.y * target["height"] / height,
                        "width": block.box.width * target["width"] / width,
                        "height": block.box.height * target["height"] / height,
                    }
                    locator = {
                        **origin,
                        **(
                            {"boxes": [box]}
                            if needs_render or locator_type in {"pdf", "image", "pptx"}
                            else {}
                        ),
                    }
                    elements.append(
                        ParsedElement(
                            kind="paragraph",
                            text=block.text,
                            payload={
                                "text_origin": "ocr",
                                "confidence": block.confidence,
                                "model_key": model_key,
                            },
                            locator_type=locator_type,
                            locator=locator,
                            source_text=block.text,
                        )
                    )
            else:
                assert self.vision is not None
                vision = await self.vision.describe(data, width, height)
                raw = vision.model_dump_json().encode()
                model_key = self.vision.model_key
                locator = {
                    **origin,
                    **(
                        {"boxes": [target]}
                        if needs_render or locator_type in {"pdf", "image", "pptx"}
                        else {}
                    ),
                }
                elements.append(
                    ParsedElement(
                        kind="image",
                        text=vision.description,
                        payload={
                            "text_origin": "vision",
                            "image_type": vision.image_type,
                            "uncertainties": vision.uncertainties,
                            "model_key": model_key,
                        },
                        locator_type=locator_type,
                        locator=locator,
                        source_text=vision.description,
                    )
                )
            if sum(len(element.text) for element in elements) > self.config.max_text_chars:
                raise ProviderFailure("MEDIA_RESPONSE_INVALID")
            key, digest = self.storage.new_key(), hashlib.sha256(raw).hexdigest()
            await asyncio.to_thread(self.storage.upload, key, io.BytesIO(raw), "application/json")
            await asyncio.to_thread(self.storage.verify, key, len(raw), digest)
            return MediaPrepared(
                elements,
                ParsedArtifact(
                    file_name="payload.json",
                    artifact_type=lease.stage + "_payload",
                    content_type="application/json",
                    object_key=key,
                    size=len(raw),
                    digest=digest,
                    metadata={"model_key": model_key, "text_origin": lease.stage},
                ),
                image_artifact=image_artifact,
                image_metadata=image_metadata,
            )
        except ProviderFailure as error:
            return MediaPrepared(
                error=str(error), image_artifact=image_artifact, image_metadata=image_metadata
            )
        except Exception:
            return MediaPrepared(error="MEDIA_SOURCE_UNAVAILABLE")

    async def persist(self, session: AsyncSession, lease: "Lease", result: MediaPrepared) -> None:
        unit = await session.get(ProcessingUnit, lease.unit_id)
        source = await session.get(SourceUnit, unit.source_unit_id) if unit else None
        if source is None:
            raise ValueError("missing source")
        if result.image_artifact:
            await register_artifact(
                session, self.storage, lease.revision_id, source.id, result.image_artifact
            )
            source.metadata_ = {**(source.metadata_ or {}), **result.image_metadata}
            if source.width is None and result.image_metadata:
                source.width = result.image_metadata["image_width"]
                source.height = result.image_metadata["image_height"]
        if result.error:
            if lease.stage == "ocr":
                source.extraction_status = "failed"
            return
        old_ids = select(ContentElement.id).where(
            ContentElement.source_unit_id == source.id,
            ContentElement.structured_payload["text_origin"].astext == lease.stage,
        )
        await session.execute(delete(SourceAnchor).where(SourceAnchor.element_id.in_(old_ids)))
        await session.execute(delete(ContentElement).where(ContentElement.id.in_(old_ids)))
        last = await session.scalar(
            select(func.max(ContentElement.ordinal)).where(
                ContentElement.source_unit_id == source.id
            )
        )
        for offset, element in enumerate(result.elements, (last + 1) if last is not None else 0):
            row = ContentElement(
                source_unit_id=source.id,
                ordinal=offset,
                element_type=element.kind,
                content_text=element.text,
                structured_payload=element.payload,
                status="success",
            )
            session.add(row)
            await session.flush()
            session.add(
                SourceAnchor(
                    revision_id=lease.revision_id,
                    source_unit_id=source.id,
                    element_id=row.id,
                    locator_type=element.locator_type,
                    locator=element.locator,
                    source_text=element.source_text,
                    source_text_sha256=hashlib.sha256(element.source_text.encode()).hexdigest(),
                )
            )
            if row.element_type == "table":
                session.add(
                    ProcessingUnit(
                        revision_id=lease.revision_id,
                        latest_task_id=lease.task_id,
                        source_unit_id=source.id,
                        stage="table",
                        unit_key=f"element:{row.id}",
                    )
                )
        if lease.stage == "ocr":
            source.extraction_status = "success"
            source.extracted_text = "\n".join(
                [source.extracted_text or "", *[e.text for e in result.elements]]
            ).strip()
        if result.artifact:
            await register_artifact(
                session, self.storage, lease.revision_id, source.id, result.artifact
            )
