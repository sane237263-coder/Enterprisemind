import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_mind.ingestion.models import (
    ContentElement,
    Document,
    OutboxEvent,
    ProcessingTask,
    ProcessingUnit,
    Revision,
    SourceAnchor,
)
from enterprise_mind.knowledge.models import KnowledgeBase
from enterprise_mind.rag.chunking import (
    AnchorInput,
    ChunkDraft,
    ChunkingFailure,
    ElementInput,
    build_source,
)
from enterprise_mind.rag.config import RAGConfig
from enterprise_mind.rag.keywords import search_tokens
from enterprise_mind.rag.models import Chunk, ChunkAnchor, ChunkEmbedding
from enterprise_mind.rag.providers import EmbeddingProvider, ProviderFailure, validate_vector

if TYPE_CHECKING:
    from enterprise_mind.ingestion.worker import Lease


@dataclass
class IndexResult:
    drafts: list[ChunkDraft] | None = None
    vector: list[float] | None = None
    error: str | None = None


class IndexingService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        config: RAGConfig,
        embedding: EmbeddingProvider,
    ) -> None:
        self.sessions, self.config, self.embedding = sessions, config, embedding

    async def prepare(self, lease: "Lease") -> IndexResult:
        try:
            if lease.stage == "activate":
                return IndexResult()
            if lease.stage == "embedding":
                async with self.sessions() as session:
                    chunk = await session.get(Chunk, int(lease.unit_key.split(":")[1]))
                    if chunk is None or chunk.revision_id != lease.revision_id:
                        return IndexResult(error="CHUNK_SOURCE_INVALID")
                    content = chunk.content
                vectors = await self.embedding.embed([content])
                if len(vectors) != 1:
                    raise ProviderFailure("EMBEDDING_INVALID")
                return IndexResult(vector=validate_vector(vectors[0], self.embedding.dimensions))
            async with self.sessions() as session:
                unit = await session.get(ProcessingUnit, lease.unit_id)
                assert unit is not None
                rows = (
                    await session.execute(
                        select(ContentElement, SourceAnchor)
                        .join(SourceAnchor, SourceAnchor.element_id == ContentElement.id)
                        .where(
                            ContentElement.source_unit_id == unit.source_unit_id,
                            ContentElement.status == "success",
                            SourceAnchor.revision_id == lease.revision_id,
                        )
                        .order_by(ContentElement.ordinal, SourceAnchor.id)
                    )
                ).all()
                # The first anchor is canonical; split anchors have larger IDs.
                inputs = []
                seen: set[int] = set()
                for element, anchor in rows:
                    if element.id in seen or not anchor.source_text:
                        continue
                    seen.add(element.id)
                    if (
                        anchor.source_text_sha256
                        != hashlib.sha256(anchor.source_text.encode()).hexdigest()
                    ):
                        raise ChunkingFailure("CHUNK_SOURCE_INVALID")
                    inputs.append(
                        ElementInput(
                            element.id,
                            element.parent_element_id,
                            element.ordinal,
                            element.element_type,
                            element.content_text or "",
                            element.structured_payload or {},
                            AnchorInput(
                                anchor.id,
                                anchor.source_unit_id,
                                element.id,
                                anchor.locator_type,
                                anchor.locator,
                                anchor.source_text,
                            ),
                        )
                    )
            drafts = await asyncio.to_thread(
                build_source, inputs, self.config.chunk, self.embedding.counter
            )
            return IndexResult(drafts=drafts) if drafts else IndexResult(error="CHUNK_NO_CONTENT")
        except ChunkingFailure as error:
            return IndexResult(error=str(error))
        except ProviderFailure:
            return IndexResult(error="EMBEDDING_FAILED")

    async def persist(
        self, session: AsyncSession, lease: "Lease", document: Document, result: IndexResult
    ) -> None:
        if lease.stage == "activate":
            return
        if lease.stage == "embedding":
            chunk = await session.scalar(
                select(Chunk)
                .where(
                    Chunk.id == int(lease.unit_key.split(":")[1]),
                    Chunk.revision_id == lease.revision_id,
                )
                .with_for_update()
            )
            if chunk is None:
                return
            if result.error:
                if chunk.status == "pending":
                    chunk.status = "failed"
                return
            assert result.vector is not None
            existing = await session.scalar(
                select(ChunkEmbedding)
                .where(
                    ChunkEmbedding.chunk_id == chunk.id,
                    ChunkEmbedding.model_key == self.embedding.model_key,
                )
                .with_for_update()
            )
            if existing is None:
                session.add(
                    ChunkEmbedding(
                        chunk_id=chunk.id,
                        model_key=self.embedding.model_key,
                        dimensions=self.embedding.dimensions,
                        embedding=result.vector,
                        status="ready",
                        embedded_at=datetime.now(UTC),
                    )
                )
            else:
                existing.embedding, existing.dimensions = result.vector, self.embedding.dimensions
                existing.status, existing.error_code = "ready", None
                existing.embedded_at = existing.updated_at = datetime.now(UTC)
            keyword = await session.scalar(
                select(ProcessingUnit.id).where(
                    ProcessingUnit.revision_id == lease.revision_id,
                    ProcessingUnit.stage == "keyword_index",
                    ProcessingUnit.unit_key == lease.unit_key,
                    ProcessingUnit.status == "success",
                )
            )
            anchored = await session.scalar(
                select(ChunkAnchor.id)
                .join(SourceAnchor)
                .where(
                    ChunkAnchor.chunk_id == chunk.id,
                    SourceAnchor.revision_id == lease.revision_id,
                )
                .limit(1)
            )
            if keyword is None or anchored is None:
                raise ChunkingFailure("CHUNK_SOURCE_INVALID")
            chunk.status = "ready"
            return
        if result.error:
            return
        ordinal = await session.scalar(
            select(func.max(Chunk.ordinal)).where(Chunk.revision_id == lease.revision_id)
        )
        next_ordinal = 0 if ordinal is None else ordinal + 1
        for draft in result.drafts or []:
            # Equal text at different positions is still a different source fragment.
            identity = hashlib.sha256(
                json.dumps(
                    [
                        lease.unit_key,
                        draft.kind,
                        draft.context_group,
                        draft.content,
                        [
                            [
                                str(piece.anchor.source_unit_id),
                                str(piece.anchor.element_id),
                                piece.start,
                                piece.end,
                                piece.locator,
                                piece.quote,
                            ]
                            for piece in draft.pieces
                        ],
                    ],
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            existing_id = await session.scalar(
                select(Chunk.id)
                .where(
                    Chunk.revision_id == lease.revision_id,
                    Chunk.metadata_["processing_identity"].astext == identity,
                )
                .limit(1)
            )
            if existing_id is not None:
                continue
            # Earlier schema revisions lack this identity; compare their full anchor ranges.
            legacy = list(
                (
                    await session.scalars(
                        select(Chunk).where(
                            Chunk.revision_id == lease.revision_id,
                            Chunk.metadata_["processing_identity"].astext.is_(None),
                            Chunk.content == draft.content,
                            Chunk.chunk_type == draft.kind,
                            Chunk.context_group == draft.context_group,
                        )
                    )
                ).all()
            )
            for candidate in legacy:
                anchors = (
                    await session.execute(
                        select(ChunkAnchor, SourceAnchor)
                        .join(SourceAnchor, SourceAnchor.id == ChunkAnchor.anchor_id)
                        .where(ChunkAnchor.chunk_id == candidate.id)
                        .order_by(ChunkAnchor.ordinal)
                    )
                ).all()
                if [
                    [
                        a.source_unit_id,
                        a.element_id,
                        c.content_start,
                        c.content_end,
                        a.locator,
                        c.quote_text,
                    ]
                    for c, a in anchors
                ] == [
                    [
                        p.anchor.source_unit_id,
                        p.anchor.element_id,
                        p.start,
                        p.end,
                        p.locator,
                        p.quote,
                    ]
                    for p in draft.pieces
                ]:
                    candidate.metadata_ = {
                        **(candidate.metadata_ or {}),
                        "processing_identity": identity,
                    }
                    existing_id = candidate.id
                    break
            if existing_id is not None:
                continue
            tokens = search_tokens(draft.content)
            if not tokens:
                raise ChunkingFailure("CHUNK_NO_CONTENT")
            chunk = Chunk(
                revision_id=lease.revision_id,
                document_id=document.id,
                knowledge_base_id=document.knowledge_base_id,
                ordinal=next_ordinal,
                chunk_type=draft.kind,
                content=draft.content,
                search_tokens=tokens,
                search_vector=func.to_tsvector("simple", tokens),
                token_count=draft.token_count,
                context_group=draft.context_group,
                metadata_={**draft.metadata, "processing_identity": identity},
                status="pending",
            )
            next_ordinal += 1
            session.add(chunk)
            await session.flush()
            for order, piece in enumerate(draft.pieces):
                anchor_id = piece.anchor.id
                if piece.quote != piece.anchor.text or piece.locator != piece.anchor.locator:
                    # A narrow source anchor keeps repeated headers and split body ranges separate.
                    anchor = SourceAnchor(
                        revision_id=lease.revision_id,
                        source_unit_id=piece.anchor.source_unit_id,
                        element_id=piece.anchor.element_id,
                        locator_type=piece.anchor.locator_type,
                        locator=piece.locator,
                        source_text=piece.quote,
                        source_text_sha256=hashlib.sha256(piece.quote.encode()).hexdigest(),
                    )
                    session.add(anchor)
                    await session.flush()
                    anchor_id = anchor.id
                session.add(
                    ChunkAnchor(
                        chunk_id=chunk.id,
                        anchor_id=anchor_id,
                        ordinal=order,
                        content_start=piece.start,
                        content_end=piece.end,
                        quote_text=piece.quote,
                    )
                )
            for stage in ("keyword_index", "embedding"):
                session.add(
                    ProcessingUnit(
                        revision_id=lease.revision_id,
                        latest_task_id=lease.task_id,
                        stage=stage,
                        unit_key=f"chunk:{chunk.id}",
                        source_unit_id=draft.pieces[0].anchor.source_unit_id,
                        status="success" if stage == "keyword_index" else "pending",
                        current=1 if stage == "keyword_index" else 0,
                        total=1,
                        finished_at=datetime.now(UTC) if stage == "keyword_index" else None,
                    )
                )

    async def finalize(
        self, session: AsyncSession, document: Document, revision: Revision, task: ProcessingTask
    ) -> None:
        units = list(
            (
                await session.scalars(
                    select(ProcessingUnit).where(
                        ProcessingUnit.revision_id == revision.id,
                        ~(
                            (ProcessingUnit.stage == "preview")
                            & (ProcessingUnit.unit_key == "rebuild")
                        ),
                    )
                )
            ).all()
        )
        if any(unit.status in {"pending", "processing"} for unit in units):
            return
        ready = await session.scalar(
            select(func.count())
            .select_from(Chunk)
            .join(ChunkEmbedding)
            .where(
                Chunk.revision_id == revision.id,
                Chunk.status == "ready",
                ChunkEmbedding.model_key == self.embedding.model_key,
                ChunkEmbedding.status == "ready",
                ChunkEmbedding.dimensions == self.embedding.dimensions,
            )
        )
        failed = [unit for unit in units if unit.status == "failed"]
        status = "failed" if not ready else "partial_success" if failed else "success"
        now = datetime.now(UTC)
        base = await session.get(KnowledgeBase, document.knowledge_base_id)
        if ready and (
            document.deleted_at is not None or base is None or base.deleted_at is not None
        ):
            if not any(unit.stage == "activate" for unit in units):
                session.add(
                    ProcessingUnit(
                        revision_id=revision.id,
                        latest_task_id=task.id,
                        stage="activate",
                        unit_key="revision",
                        status="pending",
                        current=0,
                        total=1,
                    )
                )
            task.current_stage, task.stage_current, task.stage_total = "activate", 0, 1
            return
        # An older task must never replace a newer active revision.
        if ready and revision.revision_no == document.latest_revision_no:
            if document.active_revision_id and document.active_revision_id != revision.id:
                old = await session.get(Revision, document.active_revision_id)
                if old is not None:
                    old.retired_at = now
                    session.add(
                        OutboxEvent(
                            aggregate_type="document_revision",
                            aggregate_id=old.id,
                            event_type="document.revision.cleanup_requested",
                            payload={"document_id": str(document.id), "revision_id": str(old.id)},
                            deduplication_key=f"document.revision.cleanup:{old.id}",
                        )
                    )
            document.active_revision_id = revision.id
            revision.activated_at = revision.activated_at or now
        revision.processing_status = task.status = status
        if revision.revision_no == document.latest_revision_no:
            document.processing_status = status
        revision.failure_summary = (
            {
                "failed_unit_count": len(failed),
                "stages": sorted({unit.stage for unit in failed}),
                "units": [
                    {
                        "stage": unit.stage,
                        "source_unit_id": str(unit.source_unit_id) if unit.source_unit_id else None,
                        "error_code": unit.error_code,
                    }
                    for unit in failed
                ],
            }
            if failed
            else None
        )
        task.finished_at = task.updated_at = revision.updated_at = document.updated_at = now
        task.current_stage, task.stage_current, task.stage_total = "activate", 1, 1
        task.error_code = failed[0].error_code if failed else None
        task.error_message = failed[0].error_message if failed else None
        if not any(unit.stage == "activate" for unit in units):
            session.add(
                ProcessingUnit(
                    revision_id=revision.id,
                    latest_task_id=task.id,
                    stage="activate",
                    unit_key="revision",
                    status="success" if ready else "failed",
                    current=1,
                    total=1,
                    finished_at=now,
                    error_code=None if ready else "NO_RETRIEVABLE_CONTENT",
                )
            )
