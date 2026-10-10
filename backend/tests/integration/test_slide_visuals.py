# ruff: noqa: F811
import pytest
from media_samples import visual_pptx_bytes
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ingestion import ingestion_runtime  # noqa: F401
from test_lifecycle import data, post
from test_media_pipeline import media_indexed, process  # noqa: F401
from test_preview_rebuild import descriptor, event_for, indexed_snapshot, rebuild
from test_rag_pipeline import indexed  # noqa: F401

from enterprise_mind.ingestion.models import (
    Document,
    DocumentArtifact,
    ProcessingUnit,
    SourceAnchor,
)
from enterprise_mind.rag.providers import ProviderFailure

pytestmark = pytest.mark.integration


class RenderFixture:
    calls = 0
    fail = False

    async def render(self, source, extension, destination, *, unit=None):
        import pymupdf

        self.calls += 1
        if self.fail:
            raise ProviderFailure("OFFICE_CONVERSION_FAILED")
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=720, height=540)
            page.draw_rect((70, 140, 220, 220))
            page.insert_text((85, 170), "ABC-123 Request")
            pdf.save(destination)


@pytest.mark.parametrize(
    "options",
    [{}, {"chart": True}, {"smartart": True}, {"inherited": "layout"}, {"inherited": "master"}],
)
def test_slide_visual_pipeline_anchors_and_preview(media_indexed, database, options):
    indexed, ocr, vision = media_indexed
    runtime = indexed[0]
    converter = RenderFixture()
    runtime.worker.media.converter = converter
    identifier = process(runtime, "pptx", visual_pptx_bytes(**options))["document_id"]
    assert converter.calls == 1 and ocr.calls == 1 and vision.calls == 1
    with Session(database.engine) as session:
        assert session.get(Document, int(identifier)).processing_status == "success"
        anchor = session.scalar(
            select(SourceAnchor).where(SourceAnchor.source_text.like("图片语义%"))
        )
        assert anchor.locator_type == "pptx" and anchor.locator["slide"] == 1
        assert anchor.locator["shape_id"] == "slide-visual"
        anchor_id = anchor.id
    preview = data(
        runtime.client.get(
            f"/api/v1/documents/{identifier}/preview?anchor_id={anchor_id}", headers=runtime.admin
        )
    )
    assert preview["highlight"]["text_origin"] == "vision"
    assert len(preview["visuals"]) == 1
    image = runtime.client.get(preview["visuals"][0]["content_url"], headers=runtime.admin)
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    before = indexed_snapshot(database, identifier)
    task = data(rebuild(runtime, identifier), 202)
    assert runtime.run(runtime.worker.handle_event, event_for(database, task["processing_task_id"]))
    rendered = descriptor(runtime, identifier)
    html = runtime.client.get(rendered["artifacts"][0]["content_url"], headers=runtime.admin).text
    assert 'src="data:image/png;base64,' in html and "图片语义" in html
    assert indexed_snapshot(database, identifier) == before
    assert converter.calls == 1 and ocr.calls == 1 and vision.calls == 1


def test_render_failure_retains_native_text_and_retry_adds_visuals(media_indexed, database):
    indexed, ocr, vision = media_indexed
    runtime = indexed[0]
    converter = RenderFixture()
    converter.fail = True
    runtime.worker.media.converter = converter
    identifier = process(runtime, "pptx", visual_pptx_bytes())["document_id"]
    assert ocr.calls == vision.calls == 0
    with Session(database.engine) as session:
        assert session.get(Document, int(identifier)).processing_status == "partial_success"
        failed = list(
            session.scalars(select(ProcessingUnit).where(ProcessingUnit.status == "failed"))
        )
        assert {unit.error_code for unit in failed} == {"OFFICE_CONVERSION_FAILED"}
    converter.fail = False
    retry = data(post(runtime, f"documents/{identifier}/processing/retry", {"row_version": 1}), 202)
    assert runtime.run(
        runtime.worker.handle_event, event_for(database, retry["processing_task_id"])
    )
    assert ocr.calls == vision.calls == 1
    with Session(database.engine) as session:
        assert session.get(Document, int(identifier)).processing_status == "success"
        assert (
            len(
                list(
                    session.scalars(
                        select(DocumentArtifact).where(
                            DocumentArtifact.artifact_type == "page_image"
                        )
                    )
                )
            )
            == 1
        )
