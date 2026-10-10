# ruff: noqa: F811
import pytest
from office_visual_samples import excel_visual_bytes, word_visual_bytes
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ingestion import ingestion_runtime  # noqa: F401
from test_lifecycle import data, post
from test_media_pipeline import media_indexed, process  # noqa: F401
from test_preview_rebuild import descriptor, event_for, indexed_snapshot, rebuild
from test_rag_pipeline import indexed  # noqa: F401
from test_slide_visuals import RenderFixture

from enterprise_mind.ingestion.models import Document, ProcessingUnit, SourceAnchor

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "extension,builder", [("docx", word_visual_bytes), ("xlsx", excel_visual_bytes)]
)
def test_native_office_visual_retry_preview_and_source(media_indexed, database, extension, builder):
    indexed, ocr, vision = media_indexed
    runtime = indexed[0]
    converter = RenderFixture()
    converter.fail = True
    runtime.worker.media.converter = converter
    identifier = process(runtime, extension, builder())["document_id"]
    with Session(database.engine) as session:
        assert session.get(Document, int(identifier)).processing_status == "partial_success"
        assert session.scalar(select(ProcessingUnit).where(ProcessingUnit.status == "failed"))
    assert ocr.calls == vision.calls == 0
    converter.fail = False
    retry = data(post(runtime, f"documents/{identifier}/processing/retry", {"row_version": 1}), 202)
    runtime.run(runtime.worker.handle_event, event_for(database, retry["processing_task_id"]))
    assert ocr.calls == vision.calls == 1
    with Session(database.engine) as session:
        assert session.get(Document, int(identifier)).processing_status == "success"
        anchor = session.scalar(
            select(SourceAnchor).where(SourceAnchor.source_text.like("图片语义%"))
        )
        assert anchor.locator_type == extension
        assert anchor.locator["paragraph_ordinal" if extension == "docx" else "sheet_ordinal"] == 2
        anchor_id = anchor.id
    preview = data(
        runtime.client.get(
            f"/api/v1/documents/{identifier}/preview?anchor_id={anchor_id}", headers=runtime.admin
        )
    )
    assert preview["highlight"]["width"] > 0
    assert preview["highlight"]["locator"]["boxes"]
    assert len(preview["visuals"]) == 1
    assert (
        runtime.client.get(preview["visuals"][0]["content_url"], headers=runtime.admin).status_code
        == 200
    )
    before = indexed_snapshot(database, identifier)
    task = data(rebuild(runtime, identifier), 202)
    runtime.run(runtime.worker.handle_event, event_for(database, task["processing_task_id"]))
    rendered = descriptor(runtime, identifier)
    html = runtime.client.get(rendered["artifacts"][0]["content_url"], headers=runtime.admin).text
    assert 'src="data:image/png;base64,' in html and "图片语义" in html
    assert before == indexed_snapshot(database, identifier)
