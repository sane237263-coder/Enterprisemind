import csv
import io
from pathlib import Path
from typing import Any

from charset_normalizer import from_bytes
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

from enterprise_mind.infrastructure.settings import WorkerConfig


class ParsedElement(BaseModel):
    kind: str
    text: str
    payload: dict[str, Any] = Field(default_factory=dict)
    locator_type: str
    locator: dict[str, Any]
    source_text: str
    parent: int | None = None


class ParsedSource(BaseModel):
    kind: str
    ordinal: int
    text: str
    label: str | None = None
    width: float | None = None
    height: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    elements: list[ParsedElement] = Field(default_factory=list)


class ParsedArtifact(BaseModel):
    file_name: str = Field(pattern=r"^[a-zA-Z0-9_-]+\.[a-z0-9]+$")
    artifact_type: str
    content_type: str
    source_index: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    object_key: str | None = None
    size: int = 0
    digest: str | None = None


class ParsedDocument(BaseModel):
    sources: list[ParsedSource]
    artifacts: list[ParsedArtifact] = Field(default_factory=list)


def decode_text(data: bytes) -> tuple[str, str]:
    encoding = "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    try:
        value = data.decode(encoding)
    except UnicodeDecodeError:
        match = from_bytes(data[:65536]).best()
        if match is None or match.chaos > 0.1:
            raise ValueError("invalid text encoding") from None
        encoding = match.encoding
        value = data.decode(encoding)
    if not value.strip() or any(ord(char) < 32 and char not in "\t\n\r\f" for char in value):
        raise ValueError("invalid text content")
    return value.replace("\r\n", "\n").replace("\r", "\n"), encoding


def text_locator(lines: list[str], start: int, end: int, offsets: list[int]) -> dict[str, Any]:
    return {
        "start_line": start + 1,
        "end_line": end,
        "char_start": offsets[start],
        "char_end": offsets[end],
    }


def parse_text(value: str, markdown: bool) -> ParsedSource:
    source = ParsedSource(kind="document", ordinal=0, text=value)
    lines = value.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    if not markdown:
        start = 0
        for end in range(1, len(lines) + 1):
            if not lines[end - 1].strip() or end == len(lines):
                raw = "".join(lines[start:end])
                if raw.strip():
                    source.elements.append(
                        ParsedElement(
                            kind="paragraph",
                            text=raw,
                            payload={"text_origin": "native"},
                            locator_type="text",
                            locator=text_locator(lines, start, end, offsets),
                            source_text=raw,
                        )
                    )
                start = end
        return source
    tokens = MarkdownIt("default", {"html": False}).parse(value)
    headings: list[tuple[int, int]] = []
    list_stack: list[bool] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type in {"bullet_list_open", "ordered_list_open"}:
            list_stack.append(token.type == "ordered_list_open")
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            list_stack.pop()
        elif token.map and token.type in {
            "heading_open",
            "paragraph_open",
            "fence",
            "code_block",
            "table_open",
        }:
            start, end = token.map
            raw = "".join(lines[start:end])
            kind, content = "paragraph", raw
            payload: dict[str, Any] = {"text_origin": "native"}
            if token.type == "heading_open":
                kind, content = "heading", tokens[index + 1].content
                level = int(token.tag[1:])
                payload = {"level": level}
                while headings and headings[-1][0] >= level:
                    headings.pop()
            elif token.type in {"fence", "code_block"}:
                kind, content = "code", token.content
                payload = {
                    "language": token.info.split()[0] if token.info.strip() else "plain_text"
                }
            elif token.type == "table_open":
                kind = "table"
                rows: list[list[str]] = []
                row: list[str] = []
                finish = index + 1
                while tokens[finish].type != "table_close":
                    if tokens[finish].type == "tr_open":
                        row = []
                    elif tokens[finish].type == "inline":
                        row.append(tokens[finish].content)
                    elif tokens[finish].type == "tr_close":
                        rows.append(row)
                    finish += 1
                payload = {
                    "table_name": None,
                    "headers": rows[0],
                    "rows": rows[1:],
                    "merged_ranges": [],
                    "source_range": text_locator(lines, start, end, offsets),
                }
                index = finish
            elif list_stack:
                kind, content = "list", tokens[index + 1].content
                payload = {"list_level": len(list_stack), "ordered": list_stack[-1]}
            elif token.type == "paragraph_open":
                content = tokens[index + 1].content
            parent = headings[-1][1] if headings else None
            source.elements.append(
                ParsedElement(
                    kind=kind,
                    text=content,
                    payload=payload,
                    locator_type="text",
                    locator=text_locator(lines, start, end, offsets),
                    source_text=raw,
                    parent=parent,
                )
            )
            if kind == "heading":
                headings.append((int(token.tag[1:]), len(source.elements) - 1))
        index += 1
    return source


def column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def parse_csv(value: str, config: WorkerConfig) -> ParsedSource:
    csv.field_size_limit(config.max_text_chars)
    try:
        dialect = csv.Sniffer().sniff(value[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = []
    for row in csv.reader(io.StringIO(value, newline=""), dialect=dialect, strict=True):
        rows.append(row)
        if len(rows) > config.max_elements or len(row) > 16384:
            raise ValueError("CSV structure limit")
    if not rows or not rows[0]:
        raise ValueError("empty CSV")
    width = max(map(len, rows))
    locator = {
        "sheet": "CSV",
        "sheet_ordinal": 1,
        "cell_range": f"A1:{column_name(width)}{len(rows)}",
    }
    source = ParsedSource(kind="sheet", ordinal=0, label="CSV", text=value)
    source.elements.append(
        ParsedElement(
            kind="table",
            text=value,
            payload={
                "sheet": "CSV",
                "table_name": None,
                "headers": rows[0],
                "rows": rows[1:],
                "merged_ranges": [],
                "source_range": locator,
            },
            locator_type="xlsx",
            locator=locator,
            source_text=value,
        )
    )
    return source


def parse_pdf(path: Path, config: WorkerConfig) -> list[ParsedSource]:
    import pymupdf

    pdf: Any = pymupdf  # PyMuPDF exposes a dynamically generated, untyped native API.
    pdf.TOOLS.mupdf_display_errors(False)
    pdf.TOOLS.mupdf_display_warnings(False)
    sources = []
    total_chars = 0
    with pdf.open(path) as document:
        if not document.is_pdf or document.needs_pass or len(document) > config.max_source_units:
            raise ValueError("invalid or protected PDF")
        for ordinal, page in enumerate(document):
            # Text extraction is unrotated; normalize boxes into the displayed page coordinates.
            source = ParsedSource(
                kind="page",
                ordinal=ordinal,
                text="",
                width=page.rect.width,
                height=page.rect.height,
            )
            from enterprise_mind.ingestion.rich_parsing import table_element

            table_boxes = []
            for table in page.find_tables().tables:
                rows = [
                    ["" if value is None else value for value in row] for row in table.extract()
                ]
                if not rows or not rows[0]:
                    continue
                rectangle = (pdf.Rect(table.bbox) * page.rotation_matrix) & page.rect
                table_boxes.append(pdf.Rect(table.bbox))
                locator = {
                    "page": ordinal + 1,
                    "boxes": [
                        {
                            "x": rectangle.x0,
                            "y": rectangle.y0,
                            "width": rectangle.width,
                            "height": rectangle.height,
                        }
                    ],
                }
                merged = []
                xs = sorted({v for box in table.cells if box for v in (box[0], box[2])})
                ys = sorted({v for box in table.cells if box for v in (box[1], box[3])})
                for row in table.rows:
                    for cell in row.cells:
                        if cell is None:
                            continue
                        # Rectangles spanning grid boundaries represent merged cells.
                        c1, c2 = xs.index(cell[0]), xs.index(cell[2])
                        r1, r2 = ys.index(cell[1]), ys.index(cell[3])
                        if c2 - c1 > 1 or r2 - r1 > 1:
                            merged.append(f"{column_name(c1 + 1)}{r1 + 1}:{column_name(c2)}{r2}")
                source.elements.append(
                    table_element(rows, "pdf", locator, list(dict.fromkeys(merged)))
                )
            blocks = page.get_text(
                "blocks",
                flags=pymupdf.TEXTFLAGS_BLOCKS & ~pymupdf.TEXT_PRESERVE_IMAGES,
                sort=True,
            )
            text_parts = []
            for block in blocks:
                if block[6] != 0 or not block[4].strip():
                    continue
                if any(pdf.Rect(block[:4]) in box for box in table_boxes):
                    continue
                rectangle = (pdf.Rect(block[:4]) * page.rotation_matrix) & page.rect
                if rectangle.is_empty:
                    continue
                content = block[4]
                total_chars += len(content)
                if total_chars > config.max_text_chars:
                    raise ValueError("text limit")
                box = {
                    "x": rectangle.x0,
                    "y": rectangle.y0,
                    "width": rectangle.width,
                    "height": rectangle.height,
                }
                source.elements.append(
                    ParsedElement(
                        kind="paragraph",
                        text=content,
                        payload={"text_origin": "native"},
                        locator_type="pdf",
                        locator={"page": ordinal + 1, "boxes": [box]},
                        source_text=content,
                    )
                )
                text_parts.append(content)
            source.text = "\n".join(element.text for element in source.elements)
            images = page.get_image_info()
            source.metadata = {
                "needs_ocr": not bool(source.text.strip()),
                "needs_vision": bool(images) or (len(page.get_drawings()) >= 3 and not table_boxes),
                "rotation": page.rotation,
            }
            for item in images:
                rectangle = (pdf.Rect(item["bbox"]) * page.rotation_matrix) & page.rect
                if not rectangle.is_empty:
                    source.elements.append(
                        ParsedElement(
                            kind="image",
                            text="",
                            payload={},
                            locator_type="pdf",
                            locator={
                                "page": ordinal + 1,
                                "boxes": [
                                    {
                                        "x": rectangle.x0,
                                        "y": rectangle.y0,
                                        "width": rectangle.width,
                                        "height": rectangle.height,
                                    }
                                ],
                            },
                            source_text="",
                        )
                    )
            sources.append(source)
    return sources


def parse_file(
    path: Path, extension: str, config: WorkerConfig, artifact_dir: Path | None = None
) -> ParsedDocument:
    if extension in {"docx", "pptx", "xlsx", "png", "jpg", "jpeg", "webp"}:
        from enterprise_mind.ingestion.rich_parsing import parse_rich

        return parse_rich(path, extension, config, artifact_dir)
    if extension == "pdf":
        sources = parse_pdf(path, config)
    else:
        value, encoding = decode_text(path.read_bytes())
        if len(value) > config.max_text_chars:
            raise ValueError("text limit")
        source = (
            parse_csv(value, config)
            if extension == "csv"
            else parse_text(value, extension in {"md", "markdown"})
        )
        source.metadata["encoding"] = encoding
        sources = [source]
    if (
        not sources
        or len(sources) > config.max_source_units
        or sum(len(s.elements) for s in sources) > config.max_elements
        or sum(len(s.text) for s in sources) > config.max_text_chars
    ):
        raise ValueError("structure limit")
    result = ParsedDocument(sources=sources)
    if artifact_dir is not None:
        from enterprise_mind.ingestion.rich_parsing import finish_artifacts

        finish_artifacts(result, path, extension, config, artifact_dir)
    return result
