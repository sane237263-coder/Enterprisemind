"""Request-local citation handles; message persistence belongs to the conversation phase."""

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.rag.repository import RetrievalRepository, Scope
from enterprise_mind.rag.retrieval import RetrievalResult


@dataclass(frozen=True)
class CitationCandidate:
    key: str
    chunk_id: int
    anchor_id: int
    quote: str


@dataclass(frozen=True)
class Citation:
    chunk_id: int
    anchor_id: int
    document_id: int
    revision_id: int
    title: str
    locator_type: str
    locator: dict[str, Any]
    quote_text: str


def citation_candidates(result: RetrievalResult) -> dict[str, CitationCandidate]:
    candidates = {}
    seen = set()
    for block in result.contexts:
        for chunk in block.candidates:
            for anchor in chunk.anchors:
                pair = (chunk.id, anchor.id)
                if pair in seen:
                    continue
                seen.add(pair)
                key = "source_" + uuid4().hex
                candidates[key] = CitationCandidate(key, chunk.id, anchor.id, anchor.quote)
    return candidates


async def resolve_citation(
    repository: RetrievalRepository,
    identity: CurrentIdentity,
    candidates: dict[str, CitationCandidate],
    key: str,
    quote: str,
    *,
    scope: Scope | None = None,
) -> Citation:
    chosen = candidates.get(key)
    if chosen is None or not quote.strip() or quote not in chosen.quote:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    loaded = await repository.load(identity, scope or Scope(), [chosen.chunk_id])
    chunk = loaded.get(chosen.chunk_id)
    if chunk is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    anchor = next((a for a in chunk.anchors if a.id == chosen.anchor_id and quote in a.quote), None)
    if anchor is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    allowed_keys = {
        "text": {"start_line", "end_line", "char_start", "char_end"},
        "pdf": {"page", "boxes"},
        "image": {"boxes"},
        "xlsx": {"sheet", "sheet_ordinal", "cell_range", "table_name", "object_id"},
        "docx": {
            "heading_path",
            "paragraph_ordinal",
            "paragraph_id",
            "char_start",
            "char_end",
            "object_id",
        },
        "pptx": {"slide", "shape_id", "boxes", "char_start", "char_end"},
    }
    locator = {
        name: value
        for name, value in anchor.locator.items()
        if name in allowed_keys[anchor.locator_type]
    }
    return Citation(
        chunk.id,
        anchor.id,
        chunk.document_id,
        chunk.revision_id,
        chunk.title,
        anchor.locator_type,
        locator,
        quote,
    )
