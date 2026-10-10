"""Authenticated previews and downloads. Storage locations never leave the server."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from enterprise_mind.api.auth import Envelope, ResponseMeta, SessionDependency
from enterprise_mind.api.documents import detached_identity
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.api.lifecycle_schemas import ResourceVersion
from enterprise_mind.api.management import Cursor, Identity, Key, Limit
from enterprise_mind.api.management_schemas import Identifier
from enterprise_mind.api.pagination import Page, PageMeta, Pagination
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.identity.management import idempotent_create
from enterprise_mind.identity.security import parse_id
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.models import (
    ContentElement,
    Document,
    DocumentArtifact,
    Revision,
    SourceAnchor,
    SourceUnit,
    StorageObject,
)
from enterprise_mind.ingestion.preview_rebuild import rebuild_status, request_rebuild
from enterprise_mind.ingestion.service import visible_document
from enterprise_mind.knowledge import lifecycle

router = APIRouter(prefix="/api/v1", tags=["previews"])
DetachedIdentity = Annotated[CurrentIdentity, Depends(detached_identity)]


async def visible_revision(
    session: AsyncSession,
    identity: CurrentIdentity,
    document_id: int,
    revision_id: int | None = None,
) -> Revision:
    document = await visible_document(session, identity, document_id)
    revision = await session.scalar(
        select(Revision).where(
            Revision.document_id == document.id,
            Revision.id == (revision_id or document.active_revision_id)
            if (revision_id or document.active_revision_id)
            else Revision.revision_no == document.latest_revision_no,
        )
    )
    if (
        revision is None
        or revision.retired_at is not None
        or (
            revision.id != document.active_revision_id
            and revision.revision_no != document.latest_revision_no
        )
    ):
        raise APIError(ErrorCode.SOURCE_UNAVAILABLE)
    return revision


async def preview_data(
    session: AsyncSession,
    identity: CurrentIdentity,
    document_id: int,
    revision_id: int | None = None,
    anchor_id: int | None = None,
) -> dict[str, Any]:
    revision = await visible_revision(session, identity, document_id, revision_id)
    document = await visible_document(session, identity, document_id)
    artifacts = list(
        (
            await session.scalars(
                select(DocumentArtifact).where(
                    DocumentArtifact.revision_id == revision.id,
                    DocumentArtifact.status == "ready",
                    DocumentArtifact.artifact_type.in_(("preview_html", "preview_pdf")),
                )
            )
        ).all()
    )
    output: dict[str, Any] = {
        "document_id": str(document_id),
        "revision_id": str(revision.id),
        "ready": bool(artifacts),
        "row_version": document.row_version,
        "can_rebuild": document.active_revision_id == revision.id,
        "rebuild_task": await rebuild_status(session, revision.id),
        "visuals": [],
        "artifacts": [
            {
                "id": str(a.id),
                "type": a.artifact_type,
                "content_url": f"/api/v1/document-artifacts/{a.id}/content",
            }
            for a in artifacts
        ],
        "source_count": await session.scalar(
            select(func.count())
            .select_from(SourceUnit)
            .where(SourceUnit.revision_id == revision.id)
        ),
        "download_url": f"/api/v1/documents/{document_id}/download?revision_id={revision.id}",
    }
    sources = list(
        (
            await session.scalars(
                select(SourceUnit.id)
                .where(SourceUnit.revision_id == revision.id)
                .order_by(SourceUnit.id)
            )
        ).all()
    )
    source_positions = {source_id: index for index, source_id in enumerate(sources)}
    rendered_images = (
        await session.execute(
            select(DocumentArtifact, SourceUnit.id, SourceUnit.label, SourceUnit.metadata_)
            .join(SourceUnit, SourceUnit.id == DocumentArtifact.source_unit_id)
            .where(
                DocumentArtifact.revision_id == revision.id,
                DocumentArtifact.artifact_type == "page_image",
                DocumentArtifact.status == "ready",
            )
            .order_by(SourceUnit.id)
        )
    ).all()
    for artifact_row, source_id, source_label, source_metadata in rendered_images:
        if (source_metadata or {}).get("needs_render"):
            output["visuals"].append(
                {
                    "html_target": f"source-{source_positions[source_id]}",
                    "content_url": f"/api/v1/document-artifacts/{artifact_row.id}/content",
                    "label": source_label or "幻灯片图像",
                }
            )
    if anchor_id is not None:
        anchor = await session.get(SourceAnchor, anchor_id)
        if anchor is None or anchor.revision_id != revision.id:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        source = await session.get(SourceUnit, anchor.source_unit_id)
        element = (
            await session.get(ContentElement, anchor.element_id) if anchor.element_id else None
        )
        assert source is not None
        position = await session.scalar(
            select(func.count())
            .select_from(SourceUnit)
            .where(SourceUnit.revision_id == revision.id, SourceUnit.id < source.id)
        )
        origin = (
            (element.structured_payload or {}).get("text_origin", "native") if element else "native"
        )
        fragment = f"source-{position}"
        if element and origin == "native":
            fragment += f"-element-{element.ordinal}"
        output["highlight"] = {
            "source_unit_id": str(source.id),
            "locator_type": anchor.locator_type,
            "locator": anchor.locator,
            "width": float(source.width) if source.width else None,
            "height": float(source.height) if source.height else None,
            "text_origin": origin,
            "source_text": anchor.source_text,
            "html_target": fragment,
        }
        # PPT image OCR boxes are in slide coordinates. The HTML preview embeds
        # the image alone, so clients need its original bounds to project boxes.
        image_boxes = (source.metadata_ or {}).get("origin_locator", {}).get("boxes")
        if image_boxes and source.unit_type == "image":
            output["highlight"]["image_bounds"] = image_boxes[0]
        for artifact in output["artifacts"]:
            artifact["content_url"] += "#" + (
                f"page={anchor.locator['page']}" if artifact["type"] == "preview_pdf" else fragment
            )
    return output


class PreviewRebuildInput(ResourceVersion):
    revision_id: Identifier


@router.post("/documents/{document_id}/preview/rebuild", status_code=202)
async def rebuild(
    document_id: Identifier,
    body: PreviewRebuildInput,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    key: Key,
) -> Envelope[dict[str, Any]]:
    identity = await lifecycle.write_guard(session, identity)
    document = await lifecycle.resource(
        session, identity, "document", parse_id(document_id), recycled=False
    )
    assert isinstance(document, Document)

    async def create() -> dict[str, Any]:
        lifecycle.version(document, body.row_version)
        return await request_rebuild(session, document, parse_id(body.revision_id))

    result = await idempotent_create(
        session,
        identity,
        key,
        f"document.preview:{document_id}",
        body,
        request.app.state.tokens,
        create,
    )
    return Envelope(data=result, meta=ResponseMeta(request_id=request.state.request_id))


@router.get("/documents/{document_id}/preview")
async def preview(
    document_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    revision_id: Identifier | None = None,
    anchor_id: Identifier | None = None,
) -> Envelope[dict[str, Any]]:
    data = await preview_data(
        session,
        identity,
        parse_id(document_id),
        parse_id(revision_id) if revision_id else None,
        parse_id(anchor_id) if anchor_id else None,
    )
    return Envelope(data=data, meta=ResponseMeta(request_id=request.state.request_id))


@router.get("/documents/{document_id}/source-units")
async def source_units(
    document_id: Identifier,
    request: Request,
    session: SessionDependency,
    identity: Identity,
    revision_id: Identifier | None = None,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> Page[dict[str, Any]]:
    revision = await visible_revision(
        session, identity, parse_id(document_id), parse_id(revision_id) if revision_id else None
    )
    paging = Pagination(
        request.app.state.tokens,
        {"resource": "source_units", "actor": str(identity.user.id), "revision": str(revision.id)},
    )
    sources = list(
        (
            await session.scalars(
                select(SourceUnit)
                .where(SourceUnit.revision_id == revision.id, SourceUnit.id > paging.decode(cursor))
                .order_by(SourceUnit.id)
                .limit(limit + 1)
            )
        ).all()
    )
    rows = []
    for source in sources[:limit]:
        image = await session.scalar(
            select(DocumentArtifact.id).where(
                DocumentArtifact.source_unit_id == source.id,
                DocumentArtifact.artifact_type == "page_image",
                DocumentArtifact.status == "ready",
            )
        )
        rows.append(
            {
                "id": str(source.id),
                "type": source.unit_type,
                "ordinal": source.ordinal + 1,
                "label": source.label,
                "width": float(source.width) if source.width else None,
                "height": float(source.height) if source.height else None,
                "extraction_status": source.extraction_status,
                "image_url": f"/api/v1/document-artifacts/{image}/content" if image else None,
            }
        )
    return Page(
        data=rows,
        meta=PageMeta(
            request_id=request.state.request_id,
            next_cursor=paging.encode(sources[limit - 1].id) if len(sources) > limit else None,
        ),
    )


async def file_response(
    request: Request,
    identity: CurrentIdentity,
    obj: StorageObject,
    revision: Revision,
    *,
    inline: bool,
    filename: str,
) -> FileResponse:
    storage = request.app.state.storage
    if (
        storage is None
        or obj.status != "available"
        or obj.bucket_name != storage.bucket
        or not obj.content_sha256
    ):
        raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
    directory = TemporaryDirectory(prefix="em-preview-")
    destination = Path(directory.name) / "content"
    try:
        await asyncio.to_thread(
            storage.download, obj.object_key, destination, obj.size_bytes, obj.content_sha256
        )
        # Revalidate after storage I/O; do not hold a database transaction during transfer.
        async with request.app.state.sessions() as session, session.begin():
            current = await AuthorizationService.revalidate(session, identity)
            await visible_revision(session, current, revision.document_id, revision.id)
        return FileResponse(
            destination,
            media_type=obj.content_type,
            filename=filename,
            content_disposition_type="inline" if inline else "attachment",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox"
                ),
            },
            background=BackgroundTask(directory.cleanup),
        )
    except BaseException:
        directory.cleanup()
        raise


@router.get("/document-artifacts/{artifact_id}/content")
async def artifact_content(
    artifact_id: Identifier, request: Request, identity: DetachedIdentity
) -> FileResponse:
    async with request.app.state.sessions() as session, session.begin():
        artifact = await session.get(DocumentArtifact, parse_id(artifact_id))
        if (
            artifact is None
            or artifact.status != "ready"
            or artifact.artifact_type
            not in {
                "preview_html",
                "preview_pdf",
                "page_image",
                "thumbnail",
            }
        ):
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        revision = await session.get(Revision, artifact.revision_id)
        if revision is None:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        await visible_revision(session, identity, revision.document_id, revision.id)
        obj = await session.get(StorageObject, artifact.storage_object_id)
        if obj is None:
            raise APIError(ErrorCode.SOURCE_UNAVAILABLE)
    suffix = (
        "html"
        if artifact.artifact_type == "preview_html"
        else "pdf"
        if artifact.artifact_type == "preview_pdf"
        else "png"
    )
    return await file_response(
        request, identity, obj, revision, inline=True, filename="preview." + suffix
    )


@router.get("/documents/{document_id}/download")
async def download(
    document_id: Identifier,
    request: Request,
    identity: DetachedIdentity,
    revision_id: Identifier | None = None,
) -> FileResponse:
    async with request.app.state.sessions() as session, session.begin():
        revision = await visible_revision(
            session, identity, parse_id(document_id), parse_id(revision_id) if revision_id else None
        )
        obj = await session.get(StorageObject, revision.original_object_id)
        if obj is None:
            raise APIError(ErrorCode.SOURCE_UNAVAILABLE)
    return await file_response(
        request, identity, obj, revision, inline=False, filename=revision.original_file_name
    )
