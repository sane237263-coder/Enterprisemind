import pytest
from media_samples import visual_pptx_bytes

from enterprise_mind.infrastructure.settings import WorkerConfig
from enterprise_mind.ingestion.parsing import parse_file
from enterprise_mind.ingestion.rich_parsing import render_pdf_page


@pytest.mark.parametrize(
    "kind,options", [("vector", {}), ("smartart", {"smartart": True}), ("chart", {"chart": True})]
)
def test_visual_objects_require_render_and_preserve_slide_locator(tmp_path, kind, options):
    path = tmp_path / "visual.pptx"
    path.write_bytes(visual_pptx_bytes(**options))
    result = parse_file(path, "pptx", WorkerConfig(), tmp_path / "artifacts")
    metadata = result.sources[0].metadata
    assert metadata["needs_render"] and metadata["needs_ocr"] and metadata["needs_vision"]
    assert metadata["origin_locator"]["slide"] == 1
    assert metadata["origin_locator"]["shape_id"] == "slide-visual"
    assert kind in [obj["kind"] for obj in metadata["visual_objects"]]
    if kind == "vector":
        assert len(metadata["visual_objects"]) == 3  # including the horizontal connector
        assert "ABC-123 Request" in result.sources[0].text
    if kind == "chart":
        assert metadata["unsupported_chart"]  # XY data must not be mislabeled as category rows.


def test_hidden_slide_stays_in_render_page_mapping(tmp_path):
    path = tmp_path / "hidden.pptx"
    path.write_bytes(visual_pptx_bytes(hidden=True))
    parsed = parse_file(path, "pptx", WorkerConfig())
    assert len(parsed.sources) == 2
    assert parsed.sources[1].metadata["origin_locator"]["slide"] == 2
    assert parsed.sources[1].metadata["slide_count"] == 2


@pytest.mark.parametrize("origin", ["layout", "master"])
def test_inherited_visuals_render_without_indexing_template_text(tmp_path, origin):
    path = tmp_path / "inherited.pptx"
    path.write_bytes(visual_pptx_bytes(inherited=origin))
    source = parse_file(path, "pptx", WorkerConfig()).sources[0]
    assert source.metadata["needs_render"]
    assert source.metadata["origin_locator"]["slide"] == 1
    assert {item["origin"] for item in source.metadata["visual_objects"]} == {origin}
    assert len(source.metadata["visual_objects"]) == 3
    assert not source.elements and not source.text


def test_template_placeholders_do_not_trigger_visual_processing(tmp_path):
    from media_samples import pptx_bytes

    path = tmp_path / "text.pptx"
    path.write_bytes(pptx_bytes())
    source = parse_file(path, "pptx", WorkerConfig()).sources[0]
    assert not source.metadata.get("needs_render")
    assert "ABC-123" in source.text


def test_renderer_rejects_page_count_drift(tmp_path):
    import pymupdf

    path = tmp_path / "wrong.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(path)
    with pytest.raises(ValueError, match="invalid PDF page"):
        render_pdf_page(path, 0, WorkerConfig(), tmp_path / "artifacts", expected_pages=2)


def test_render_service_rejects_external_relationship_before_office(tmp_path):
    import io
    from zipfile import ZipFile

    from fastapi.testclient import TestClient

    from enterprise_mind.office_converter import create_converter

    payload = io.BytesIO()
    with ZipFile(io.BytesIO(visual_pptx_bytes())) as source, ZipFile(payload, "w") as target:
        for entry in source.infolist():
            target.writestr(entry, source.read(entry))
        target.writestr(
            "ppt/_rels/external.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="image" TargetMode="External" Target="file:///tmp/private.txt"/></Relationships>',
        )
    token = tmp_path / "token"
    token.write_text("test-render-token")
    with TestClient(create_converter(token, executable="missing-must-not-execute")) as client:
        assert client.post("/render/pptx", content=payload.getvalue()).status_code == 401
        response = client.post(
            "/render/pptx",
            content=payload.getvalue(),
            headers={"Authorization": "Bearer test-render-token"},
        )
        assert response.status_code == 422
