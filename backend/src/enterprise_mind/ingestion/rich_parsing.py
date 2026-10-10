"""Bounded native extraction; never execute macros, formulas, links or embedded objects."""

import base64
import csv
import importlib
import io
import re
import warnings
from html import escape
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import ZipFile

from enterprise_mind.infrastructure.settings import WorkerConfig
from enterprise_mind.ingestion.parsing import (
    ParsedArtifact,
    ParsedDocument,
    ParsedElement,
    ParsedSource,
    column_name,
)


def check_package(
    path: Path | io.BytesIO,
    extension: str,
    config: WorkerConfig,
    *,
    forbid_external_relationships: bool = False,
) -> None:
    xml: Any = importlib.import_module("defusedxml.ElementTree")
    roots = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml", "xlsx": "xl/workbook.xml"}
    with ZipFile(path) as archive:
        members = archive.infolist()
        names = [info.filename for info in members]
        if (
            len(members) > config.max_archive_members
            or len(set(names)) != len(names)
            or roots[extension] not in names
            or "[Content_Types].xml" not in names
            or sum(info.file_size for info in members) > config.max_archive_bytes
        ):
            raise ValueError("invalid Office package")
        for info in members:
            parts = PurePosixPath(info.filename).parts
            name = info.filename.lower()
            chart_workbook = (
                extension in {"pptx", "docx"}
                and name.startswith(
                    "ppt/embeddings/" if extension == "pptx" else "word/embeddings/"
                )
                and name.endswith(".xlsx")
            )
            if (
                info.flag_bits & 1
                or info.filename.startswith("/")
                or ".." in parts
                or "\\" in name
                or ":" in name
                or any(term in name for term in ("vbaproject", "activex/"))
                or ("embeddings/" in name and not chart_workbook)
            ):
                raise ValueError("unsupported active Office content")
            if chart_workbook:
                check_package(
                    io.BytesIO(archive.read(info)),
                    "xlsx",
                    config,
                    forbid_external_relationships=forbid_external_relationships,
                )
            if name.endswith((".xml", ".rels")):
                content = archive.read(info)
                # Library XML parsers receive no DTD or entity definitions.
                root = xml.fromstring(
                    content, forbid_dtd=True, forbid_entities=True, forbid_external=True
                )
                if (
                    forbid_external_relationships
                    and name.endswith(".rels")
                    and any(node.get("TargetMode", "").lower() == "external" for node in root)
                ):
                    raise ValueError("external Office relationships cannot be rendered")
                if name == "[content_types].xml" and b"macroenabled" in content.lower():
                    raise ValueError("macro-enabled Office package")


def table_element(
    rows: list[list[str]],
    locator_type: str,
    locator: dict[str, Any],
    merged: list[str] | None = None,
    **metadata: Any,
) -> ParsedElement:
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    content = stream.getvalue()
    return ParsedElement(
        kind="table",
        text=content,
        locator_type=locator_type,
        locator=locator,
        source_text=content,
        payload={
            "headers": rows[0] if rows else [],
            "rows": rows[1:],
            "merged_ranges": merged or [],
            "source_range": locator,
            "table_name": None,
            "serialization": "csv",
            **metadata,
        },
    )


def normalized_image(raw: bytes, config: WorkerConfig) -> tuple[bytes, int, int]:
    image: Any = importlib.import_module("PIL.Image")
    ops: Any = importlib.import_module("PIL.ImageOps")
    with warnings.catch_warnings():
        warnings.simplefilter("error", image.DecompressionBombWarning)
        with image.open(io.BytesIO(raw)) as opened:
            if (
                opened.width * opened.height > config.max_image_pixels
                or getattr(opened, "n_frames", 1) != 1
            ):
                raise ValueError("image limit")
            opened.load()
            corrected = ops.exif_transpose(opened).convert("RGBA")
            background = image.new("RGBA", corrected.size, "white")
            corrected = image.alpha_composite(background, corrected).convert("RGB")
            corrected.thumbnail((config.image_max_edge, config.image_max_edge))
            output = io.BytesIO()
            corrected.save(output, format="PNG")
            return output.getvalue(), corrected.width, corrected.height


def write_image(
    result: ParsedDocument,
    source_index: int,
    raw: bytes,
    config: WorkerConfig,
    directory: Path | None,
) -> None:
    data, width, height = normalized_image(raw, config)
    source = result.sources[source_index]
    if source.width is None:
        source.width, source.height = float(width), float(height)
    source.metadata.update(image_width=width, image_height=height)
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        name = f"image-{source_index}.png"
        (directory / name).write_bytes(data)
        result.artifacts.append(
            ParsedArtifact(
                file_name=name,
                artifact_type="page_image",
                content_type="image/png",
                source_index=source_index,
                metadata={"width": width, "height": height},
            )
        )


def add_picture(
    result: ParsedDocument,
    raw: bytes,
    locator_type: str,
    locator: dict[str, Any],
    config: WorkerConfig,
    directory: Path | None,
    dimensions: tuple[float, float] | None = None,
) -> None:
    source = ParsedSource(
        kind="image",
        ordinal=sum(s.kind == "image" for s in result.sources),
        text="",
        width=dimensions[0] if dimensions else None,
        height=dimensions[1] if dimensions else None,
        metadata={
            "needs_ocr": True,
            "needs_vision": True,
            "locator_type": locator_type,
            "origin_locator": locator,
        },
    )
    result.sources.append(source)
    try:
        write_image(result, len(result.sources) - 1, raw, config, directory)
    except Exception:
        # One unsupported embedded image must not discard other pages or paragraphs.
        source.metadata["visual_error"] = "IMAGE_DECODE_FAILED"


def cell_text(cell: Any, depth: int = 0) -> str:
    if depth > 16:
        raise ValueError("nested table limit")
    parts = []
    for block in cell.iter_inner_content():
        if hasattr(block, "rows"):
            seen: set[Any] = set()
            for row in block.rows:
                values = []
                for nested in row.cells:
                    if nested._tc not in seen:
                        values.append(cell_text(nested, depth + 1))
                        seen.add(nested._tc)
                parts.append(" | ".join(values))
        else:
            parts.append(block.text)
    return "\n".join(parts)


def parse_docx(path: Path, config: WorkerConfig, directory: Path | None) -> ParsedDocument:
    from enterprise_mind.ingestion.office_visuals import docx_blocks, docx_drawings

    docx: Any = importlib.import_module("docx")
    document = docx.Document(path)
    source = ParsedSource(kind="document", ordinal=0, text="")
    result = ParsedDocument(sources=[source])
    headings: list[tuple[int, str, int]] = []
    paragraph_no = 0
    blocks = docx_blocks(document)
    visual_unit = 0
    for block, scope in blocks:
        paragraph_no += 1
        locator: dict[str, Any] = {
            "paragraph_ordinal": paragraph_no,
            "heading_path": [h[1] for h in headings],
        }
        if scope:
            locator["object_id"] = scope
        if hasattr(block, "rows"):
            rows: list[list[str]] = []
            positions: dict[Any, list[tuple[int, int]]] = {}
            for r, row in enumerate(block.rows):
                cells = [""] * row.grid_cols_before
                for c, cell in enumerate(row.cells, row.grid_cols_before):
                    positions.setdefault(cell._tc, []).append((r, c))
                    cells.append(cell_text(cell) if len(positions[cell._tc]) == 1 else "")
                cells.extend([""] * row.grid_cols_after)
                rows.append(cells)
            merged = [
                f"{column_name(min(c for _, c in group) + 1)}{min(r for r, _ in group) + 1}:"
                f"{column_name(max(c for _, c in group) + 1)}{max(r for r, _ in group) + 1}"
                for group in positions.values()
                if len(group) > 1
            ]
            locator["object_id"] = f"table-{paragraph_no}"
            element = table_element(rows, "docx", locator, merged)
        else:
            content = block.text
            level = re.search(r"(?:Heading|标题)\s*(\d+)", block.style.name or "", re.I)
            if level:
                number = int(level[1])
                while headings and headings[-1][0] >= number:
                    headings.pop()
                locator["heading_path"] = [h[1] for h in headings]
            locator.update(char_start=0, char_end=len(content))
            element = ParsedElement(
                kind="heading" if level else "paragraph",
                text=content,
                payload={"text_origin": "native", **({"level": int(level[1])} if level else {})},
                locator_type="docx",
                locator=locator,
                source_text=content,
                parent=headings[-1][2] if headings else None,
            )
            if level:
                headings.append((int(level[1]), content, len(source.elements)))
        source.elements.append(element)
        for _ in docx_drawings(block):
            result.sources.append(
                ParsedSource(
                    kind="image",
                    ordinal=sum(s.kind == "image" for s in result.sources),
                    text="",
                    label=f"段落 {paragraph_no} 视觉对象 {visual_unit + 1}",
                    metadata={
                        "needs_ocr": True,
                        "needs_vision": True,
                        "needs_render": True,
                        "render_unit": visual_unit,
                        "locator_type": "docx",
                        "origin_locator": {
                            "paragraph_ordinal": paragraph_no,
                            "heading_path": locator["heading_path"],
                            "object_id": f"visual-{visual_unit + 1}",
                        },
                    },
                )
            )
            visual_unit += 1
        for image_no, blip in enumerate(block._element.xpath(".//a:blip")):
            relation = blip.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
            )
            if relation and relation in block.part.related_parts:
                picture_locator = {k: v for k, v in locator.items() if not k.startswith("char_")}
                picture_locator["object_id"] = f"image-{paragraph_no}-{image_no}"
                add_picture(
                    result,
                    block.part.related_parts[relation].blob,
                    "docx",
                    picture_locator,
                    config,
                    directory,
                )
    source.text = "\n".join(e.text for e in source.elements)
    return result


def parse_pptx(path: Path, config: WorkerConfig, directory: Path | None) -> ParsedDocument:
    pptx: Any = importlib.import_module("pptx")
    presentation = pptx.Presentation(path)
    if len(presentation.slides) > config.max_source_units:
        raise ValueError("slide limit")
    width, height = (
        float(presentation.slide_width) / 12700,
        float(presentation.slide_height) / 12700,
    )
    result = ParsedDocument(sources=[])
    for index, slide in enumerate(presentation.slides):
        source = ParsedSource(
            kind="slide",
            ordinal=index,
            text="",
            width=width,
            height=height,
            label=f"Slide {index + 1}",
        )
        result.sources.append(source)

        def visit(
            shapes: Any,
            transform: tuple[float, float, float, float] = (1, 1, 0, 0),
            slide_index: int = index,
            target_source: ParsedSource = source,
            origin: str = "slide",
        ) -> None:
            sx, sy, ox, oy = transform
            for shape in shapes:
                if origin != "slide" and shape.is_placeholder:
                    # Template placeholders are not rendered source text for this slide.
                    continue
                if hasattr(shape, "shapes"):
                    xfrm = shape._element.grpSpPr.xfrm
                    gx, gy = (
                        xfrm.ext.cx / max(1, xfrm.chExt.cx),
                        xfrm.ext.cy / max(1, xfrm.chExt.cy),
                    )
                    visit(
                        shape.shapes,
                        (
                            sx * gx,
                            sy * gy,
                            ox + sx * (xfrm.off.x - gx * xfrm.chOff.x),
                            oy + sy * (xfrm.off.y - gy * xfrm.chOff.y),
                        ),
                        slide_index,
                        target_source,
                        origin,
                    )
                    continue
                x, y = (
                    max(0.0, (ox + sx * shape.left) / 12700),
                    max(0.0, (oy + sy * shape.top) / 12700),
                )
                box = {
                    "x": min(x, width),
                    "y": min(y, height),
                    "width": min(max(0.0, sx * shape.width / 12700), max(0.0, width - x)),
                    "height": min(max(0.0, sy * shape.height / 12700), max(0.0, height - y)),
                }
                if shape.shape_type == 9:
                    # Horizontal/vertical connectors have a zero native extent on one axis.
                    box["width"] = min(max(box["width"], 1.0), width - box["x"])
                    box["height"] = min(max(box["height"], 1.0), height - box["y"])
                if not box["width"] or not box["height"]:
                    continue
                locator = {
                    "slide": slide_index + 1,
                    "shape_id": str(shape.shape_id),
                    "boxes": [box],
                }
                # A whole-slide render preserves arrows, grouping, layout and chart semantics.
                # GraphicFrames also carry SmartArt/diagram and newer chart types that the
                # native library does not expose as a chart or text frame.
                graphic = shape._element.xpath(".//a:graphicData")
                graphic_uri = graphic[0].get("uri", "") if graphic else ""
                visual_kind = (
                    "smartart"
                    if "diagram" in graphic_uri
                    else "chart"
                    if shape.has_chart or "chart" in graphic_uri
                    else "graphic"
                    if graphic and not shape.has_table
                    else "vector"
                    if shape.shape_type in {1, 5, 6, 9}
                    else "inherited"
                    if origin != "slide"
                    and (
                        shape.shape_type == 13
                        or shape.has_table
                        or (shape.has_text_frame and shape.text.strip())
                    )
                    else None
                )
                if visual_kind:
                    objects = target_source.metadata.setdefault("visual_objects", [])
                    if len(objects) >= config.max_elements:
                        raise ValueError("slide visual object limit")
                    objects.append(
                        {
                            "kind": visual_kind,
                            "origin": origin,
                            "shape_id": str(shape.shape_id),
                            "boxes": [box],
                        }
                    )
                    target_source.metadata.update(
                        needs_ocr=True,
                        needs_vision=True,
                        needs_render=True,
                        locator_type="pptx",
                        slide_count=len(presentation.slides),
                        origin_locator={
                            "slide": slide_index + 1,
                            "shape_id": "slide-visual",
                            "boxes": [{"x": 0, "y": 0, "width": width, "height": height}],
                        },
                    )
                if origin != "slide":
                    # Detect inherited visuals conservatively, then let the Office renderer
                    # resolve visibility/overrides. Never index hidden master text directly.
                    continue
                if shape.has_table:
                    table = shape.table
                    rows = [
                        ["" if cell.is_spanned else cell.text for cell in row.cells]
                        for row in table.rows
                    ]
                    merged = [
                        f"{column_name(c + 1)}{r + 1}:"
                        f"{column_name(c + cell.span_width)}{r + cell.span_height}"
                        for r, row in enumerate(table.rows)
                        for c, cell in enumerate(row.cells)
                        if cell.is_merge_origin
                    ]
                    target_source.elements.append(table_element(rows, "pptx", locator, merged))
                elif shape.has_text_frame and shape.text.strip():
                    target_source.elements.append(
                        ParsedElement(
                            kind="paragraph",
                            text=shape.text,
                            payload={"text_origin": "native"},
                            locator_type="pptx",
                            locator={**locator, "char_start": 0, "char_end": len(shape.text)},
                            source_text=shape.text,
                        )
                    )
                if hasattr(shape, "image"):
                    add_picture(
                        result,
                        shape.image.blob,
                        "pptx",
                        locator,
                        config,
                        directory,
                        (width, height),
                    )
                elif shape.has_chart:
                    # Native chart data stays queryable even without a Vision model.
                    try:
                        chart = shape.chart
                        if len(chart.plots) != 1:
                            raise ValueError("multiple chart plots require visual interpretation")
                        categories = [str(c.label) for c in chart.plots[0].categories]
                        if not categories:
                            raise ValueError("non-category chart requires visual interpretation")
                        series = list(chart.series)
                        rows = [
                            ["类别", *[str(s.name) for s in series]],
                            *[
                                [
                                    label,
                                    *[
                                        str(s.values[n]) if n < len(s.values) else ""
                                        for s in series
                                    ],
                                ]
                                for n, label in enumerate(categories)
                            ],
                        ]
                        target_source.elements.append(
                            table_element(rows, "pptx", locator, chart=True)
                        )
                    except (
                        AttributeError,
                        IndexError,
                        TypeError,
                        ValueError,
                        KeyError,
                        NotImplementedError,
                    ):
                        target_source.metadata["unsupported_chart"] = True

        visit(slide.shapes)
        visit(slide.slide_layout.shapes, origin="layout")
        visit(slide.slide_layout.slide_master.shapes, origin="master")
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame
            if notes is not None and notes.text.strip():
                source.elements.append(
                    ParsedElement(
                        kind="paragraph",
                        text=notes.text,
                        payload={"text_origin": "native", "notes": True},
                        locator_type="pptx",
                        locator={
                            "slide": index + 1,
                            "shape_id": "notes",
                            "boxes": [{"x": 0, "y": 0, "width": width, "height": height}],
                            "char_start": 0,
                            "char_end": len(notes.text),
                        },
                        source_text=notes.text,
                    )
                )
        source.text = "\n".join(e.text for e in source.elements)
    return result


def parse_xlsx(path: Path, config: WorkerConfig, directory: Path | None) -> ParsedDocument:
    excel: Any = importlib.import_module("openpyxl")
    # data_only reads cached values; neither workbook is recalculated or linked externally.
    book = excel.load_workbook(path, data_only=False, keep_links=False)
    cached = excel.load_workbook(path, data_only=True, keep_links=False)
    result = ParsedDocument(sources=[])
    try:
        for index, sheet in enumerate(book.worksheets):
            if sheet.max_row * sheet.max_column > config.max_elements:
                raise ValueError("sheet cell limit")
            rows, formulas = [], {}
            for row in sheet.iter_rows():
                values = []
                for cell in row:
                    value = cell.value
                    if cell.data_type == "f":
                        formulas[cell.coordinate] = str(value)
                        value = cached[sheet.title][cell.coordinate].value
                        if value is None:
                            value = "[公式未缓存] " + str(cell.value)
                    values.append("" if value is None else str(value))
                rows.append(values)
            locator = {
                "sheet": sheet.title,
                "sheet_ordinal": index + 1,
                "cell_range": f"A1:{column_name(sheet.max_column)}{sheet.max_row}",
            }
            element = table_element(
                rows,
                "xlsx",
                locator,
                [str(r) for r in sheet.merged_cells.ranges],
                sheet=sheet.title,
                formulas=formulas,
                hidden_rows=[n for n, dim in sheet.row_dimensions.items() if dim.hidden],
                hidden_columns=[n for n, dim in sheet.column_dimensions.items() if dim.hidden],
            )
            result.sources.append(
                ParsedSource(
                    kind="sheet",
                    ordinal=index,
                    label=sheet.title,
                    text=element.text,
                    metadata={"sheet_state": sheet.sheet_state},
                    elements=[element],
                )
            )
            if sheet._charts:
                result.sources.append(
                    ParsedSource(
                        kind="image",
                        ordinal=sum(s.kind == "image" for s in result.sources),
                        text="",
                        label=f"{sheet.title} 图表",
                        metadata={
                            "needs_ocr": True,
                            "needs_vision": True,
                            "needs_render": True,
                            "render_unit": index,
                            "locator_type": "xlsx",
                            "origin_locator": {
                                "sheet": sheet.title,
                                "sheet_ordinal": index + 1,
                                "object_id": "sheet-visual",
                            },
                        },
                    )
                )
            for number, picture in enumerate(sheet._images):
                marker = getattr(picture.anchor, "_from", None)
                cell = (
                    f"{column_name(marker.col + 1)}{marker.row + 1}" if marker is not None else "A1"
                )
                add_picture(
                    result,
                    picture._data(),
                    "xlsx",
                    {**locator, "cell_range": f"{cell}:{cell}", "object_id": f"image-{number}"},
                    config,
                    directory,
                )
    finally:
        book.close()
        cached.close()
    return result


def preview_html(result: ParsedDocument, directory: Path) -> bytes:
    parts = [
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>文档预览</title>",
        "<style>body{font:16px/1.65 sans-serif;max-width:1100px;margin:32px auto;padding:0 24px;"
        "color:#202b38}section{margin:24px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}"
        "table{border-collapse:collapse;max-width:100%;display:block;overflow:auto}"
        "td,th{border:1px solid #ccd4df;padding:6px 10px;white-space:pre-wrap}"
        "img{max-width:100%;height:auto}:target{background:#fff3b0;outline:2px solid #b78520}"
        "</style><body>",
    ]
    for index, source in enumerate(result.sources):
        label = escape(source.label or source.kind + " " + str(source.ordinal + 1))
        parts.append(f'<section id="source-{index}"><h2>{label}</h2>')
        for ordinal, element in enumerate(source.elements):
            identifier = f"source-{index}-element-{element.payload.get('preview_ordinal', ordinal)}"
            origin = element.payload.get("text_origin", "native")
            if origin in {"vision", "ocr"}:
                label = "图片语义说明（模型生成）" if origin == "vision" else "文字识别结果（OCR）"
                parts.append(f"<p><strong>{label}</strong></p>")
            if element.kind == "table":
                rows = [element.payload.get("headers", []), *element.payload.get("rows", [])]
                parts.append(f'<table id="{identifier}">')
                utilities: Any = importlib.import_module("openpyxl.utils.cell")
                spans: dict[tuple[int, int], tuple[int, int]] = {}
                hidden: set[tuple[int, int]] = set()
                for merged in element.payload.get("merged_ranges", []):
                    c1, r1, c2, r2 = utilities.range_boundaries(merged)
                    spans[(r1 - 1, c1 - 1)] = (r2 - r1 + 1, c2 - c1 + 1)
                    hidden.update(
                        (r, c)
                        for r in range(r1 - 1, r2)
                        for c in range(c1 - 1, c2)
                        if (r, c) != (r1 - 1, c1 - 1)
                    )
                for r, row in enumerate(rows):
                    parts.append("<tr>")
                    for c, cell in enumerate(row):
                        if (r, c) in hidden:
                            continue
                        rowspan, colspan = spans.get((r, c), (1, 1))
                        parts.append(
                            f'<td rowspan="{rowspan}" colspan="{colspan}">'
                            + escape(str(cell))
                            + "</td>"
                        )
                    parts.append("</tr>")
                parts.append("</table>")
            else:
                tag = "h3" if element.kind == "heading" else "pre"
                parts.append(f'<{tag} id="{identifier}">{escape(element.text)}</{tag}>')
        artifact = next(
            (
                a
                for a in result.artifacts
                if a.source_index == index and a.artifact_type == "page_image"
            ),
            None,
        )
        if artifact is not None:
            data = base64.b64encode((directory / artifact.file_name).read_bytes()).decode("ascii")
            parts.append(f'<img alt="来源图像" src="data:image/png;base64,{data}">')
        elif source.metadata.get("needs_render"):
            parts.append(
                '<p data-visual-pending="true">本页包含图表或关系图，图像预览正在准备。</p>'
            )
        parts.append("</section>")
    parts.append("</body></html>")
    return "".join(parts).encode("utf-8")


def finish_artifacts(
    result: ParsedDocument,
    path: Path,
    extension: str,
    config: WorkerConfig,
    directory: Path,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if extension == "pdf":
        pdf: Any = importlib.import_module("pymupdf")
        with pdf.open(path) as document:
            for index, source in enumerate(result.sources):
                if source.metadata.get("needs_ocr") or source.metadata.get("needs_vision"):
                    page = document[source.ordinal]
                    scale = min(config.image_max_edge / max(page.rect.width, page.rect.height), 2.0)
                    pixmap = page.get_pixmap(matrix=pdf.Matrix(scale, scale), alpha=False)
                    write_image(result, index, pixmap.tobytes("png"), config, directory)
        # PDF remains directly previewable; it is registered against the original object.
    else:
        content = preview_html(result, directory)
        (directory / "preview.html").write_bytes(content)
        result.artifacts.append(
            ParsedArtifact(
                file_name="preview.html",
                artifact_type="preview_html",
                content_type="text/html; charset=utf-8",
            )
        )
    if (
        sum((directory / a.file_name).stat().st_size for a in result.artifacts)
        > config.max_artifact_bytes
    ):
        raise ValueError("artifact size limit")


def render_pdf_page(
    path: Path, page: int, config: WorkerConfig, directory: Path, expected_pages: int | None = None
) -> ParsedDocument:
    pdf: Any = importlib.import_module("pymupdf")
    with pdf.open(path) as document:
        if (
            document.needs_pass
            or not 0 <= page < len(document)
            or (expected_pages is not None and len(document) != expected_pages)
        ):
            raise ValueError("invalid PDF page")
        rect = document[page].rect
        source = ParsedSource(
            kind="page",
            ordinal=page,
            text="",
            width=rect.width,
            height=rect.height,
            metadata={"needs_ocr": True},
        )
    result = ParsedDocument(sources=[source])
    finish_artifacts(result, path, "pdf", config, directory)
    return result


def parse_rich(
    path: Path, extension: str, config: WorkerConfig, directory: Path | None
) -> ParsedDocument:
    if extension in {"docx", "pptx", "xlsx"}:
        check_package(path, extension, config)
        result = {"docx": parse_docx, "pptx": parse_pptx, "xlsx": parse_xlsx}[extension](
            path, config, directory
        )
    else:
        result = ParsedDocument(
            sources=[
                ParsedSource(
                    kind="image",
                    ordinal=0,
                    text="",
                    metadata={"needs_ocr": True, "needs_vision": True, "locator_type": "image"},
                )
            ]
        )
        write_image(result, 0, path.read_bytes(), config, directory)
    if (
        not result.sources
        or len(result.sources) > config.max_source_units
        or sum(len(s.elements) for s in result.sources) > config.max_elements
        or sum(len(s.text) for s in result.sources) > config.max_text_chars
    ):
        raise ValueError("structure limit")
    if directory is not None:
        finish_artifacts(result, path, extension, config, directory)
    return result
