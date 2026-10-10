import hashlib

import pymupdf
import pytest

from enterprise_mind.infrastructure.settings import WorkerConfig
from enterprise_mind.ingestion.parsing import decode_text, parse_file


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "gb18030"])
def test_text_encoding_and_unicode_offsets(tmp_path, encoding):
    value = "第一段：文档测试。\r\n\r\n第二段：来源定位。\r\n"
    path = tmp_path / "input"
    path.write_bytes(value.encode(encoding))
    source = parse_file(path, "txt", WorkerConfig()).sources[0]
    assert source.text == value.replace("\r\n", "\n")
    assert len(source.elements) == 2
    for element in source.elements:
        locator = element.locator
        assert source.text[locator["char_start"] : locator["char_end"]] == element.source_text
        assert locator["start_line"] >= 1


def test_markdown_preserves_heading_code_table_and_anchor_source(tmp_path):
    value = (
        "# 标题\n\n正文 **重点**。\n\n## 代码\n\n```python\nprint('你好')\n```\n\n"
        "| 名称 | 值 |\n| --- | --- |\n| 甲 | 1 |\n\n- 条目\n"
    )
    path = tmp_path / "input"
    path.write_text(value, encoding="utf-8")
    source = parse_file(path, "md", WorkerConfig()).sources[0]
    code = next(e for e in source.elements if e.kind == "code")
    assert code.payload["language"] == "python"
    assert source.elements[code.parent].text == "代码"
    table = next(e for e in source.elements if e.kind == "table")
    assert table.payload["headers"] == ["名称", "值"]
    assert table.payload["rows"] == [["甲", "1"]]
    assert any(e.kind == "list" for e in source.elements)
    for element in source.elements:
        span = source.text[element.locator["char_start"] : element.locator["char_end"]]
        assert element.source_text == span
        assert hashlib.sha256(span.encode()).hexdigest()


def test_csv_multiline_quotes_empty_cells_and_virtual_sheet(tmp_path):
    value = '名称,内容,数值\n甲,"第一行\n第二行",1\n乙,,2\n'
    path = tmp_path / "input"
    path.write_text(value, encoding="utf-8")
    source = parse_file(path, "csv", WorkerConfig()).sources[0]
    assert (source.kind, source.label, source.ordinal) == ("sheet", "CSV", 0)
    element = source.elements[0]
    assert element.payload["rows"] == [["甲", "第一行\n第二行", "1"], ["乙", "", "2"]]
    assert element.locator == {"sheet": "CSV", "sheet_ordinal": 1, "cell_range": "A1:C3"}
    assert element.source_text == value


def test_native_rotated_pdf_has_display_boxes_and_blank_page_waits_for_ocr(tmp_path):
    path = tmp_path / "input"
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=500)
        page.insert_text((30, 50), "Native PDF text")
        page.set_rotation(90)
        document.new_page()
        document.save(path)
    result = parse_file(path, "pdf", WorkerConfig())
    first, blank = result.sources
    assert (first.width, first.height) == (500, 300)
    assert "Native PDF text" in first.text
    assert first.metadata["needs_ocr"] is False
    for element in first.elements:
        assert element.locator["page"] == 1
        for box in element.locator["boxes"]:
            assert 0 <= box["x"] < box["x"] + box["width"] <= 500
            assert 0 <= box["y"] < box["y"] + box["height"] <= 300
    assert blank.metadata["needs_ocr"] is True


@pytest.mark.parametrize("data", [b"", b"abc\x00def", b"\x01\x02\x03"])
def test_binary_or_empty_text_rejected(data):
    with pytest.raises(ValueError):
        decode_text(data)


def test_damaged_pdf_and_parser_limits_fail(tmp_path):
    path = tmp_path / "input"
    path.write_bytes(b"%PDF-damaged")
    with pytest.raises(pymupdf.FileDataError):
        parse_file(path, "pdf", WorkerConfig())
    path.write_text("x" * 101, encoding="utf-8")
    with pytest.raises(ValueError):
        parse_file(path, "txt", WorkerConfig(max_text_chars=100))
