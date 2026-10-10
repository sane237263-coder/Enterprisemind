"""Locate native Office visuals and make bounded render copies inside the parser process."""

import importlib
from copy import deepcopy
from pathlib import Path
from typing import Any


def docx_blocks(document: Any) -> list[tuple[Any, str | None]]:
    blocks: list[tuple[Any, str | None]] = [
        (block, None) for block in document.iter_inner_content()
    ]
    seen_parts: set[str] = set()
    for number, section in enumerate(document.sections, 1):
        for kind in (
            "header",
            "first_page_header",
            "even_page_header",
            "footer",
            "first_page_footer",
            "even_page_footer",
        ):
            part = getattr(section, kind)
            if part.is_linked_to_previous or str(part.part.partname) in seen_parts:
                continue
            seen_parts.add(str(part.part.partname))
            blocks.extend((block, f"{kind}-{number}") for block in part.iter_inner_content())
    return blocks


def docx_drawings(block: Any) -> list[Any]:
    result = []
    for node in block._element.xpath(".//w:drawing | .//w:pict"):
        # Only pure raster frames use the image-only path. A native group can also
        # contain raster pictures, so inspecting for any descendant pic loses its shapes.
        graphics = list(
            node.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}graphicData")
        )
        if graphics and all(
            item.get("uri") == "http://schemas.openxmlformats.org/drawingml/2006/picture"
            for item in graphics
        ):
            continue
        result.append(node)
    return result


def render_copy(source: Path, extension: str, unit: int, destination: Path) -> None:
    if unit < 0:
        raise ValueError("invalid visual unit")
    if extension == "docx":
        docx: Any = importlib.import_module("docx")
        oxml: Any = importlib.import_module("docx.oxml")
        document = docx.Document(source)
        drawings = [
            (block, drawing)
            for block, _ in docx_blocks(document)
            for drawing in docx_drawings(block)
        ]
        if unit >= len(drawings):
            raise ValueError("visual unit not found")
        block, original = drawings[unit]
        drawing = deepcopy(original)
        relations = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
        for element in drawing.iter():
            for name, value in list(element.attrib.items()):
                if name.startswith(relations):
                    relation = block.part.rels[value]
                    if relation.is_external:
                        raise ValueError("external visual reference")
                    element.set(
                        name, document.part.relate_to(relation.target_part, relation.reltype)
                    )
        # Floating objects become inline in the isolated copy; internal geometry stays intact.
        word_drawing = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
        for anchor in list(drawing.iter(word_drawing + "anchor")):
            inline = oxml.OxmlElement("wp:inline")
            for child in list(anchor):
                if child.tag.rsplit("}", 1)[-1] in {
                    "extent",
                    "effectExtent",
                    "docPr",
                    "cNvGraphicFramePr",
                    "graphic",
                }:
                    inline.append(child)
            anchor.getparent().replace(anchor, inline)
        document._element.body.clear_content()
        section = document.sections[0]
        for reference in section._sectPr.xpath("./w:headerReference | ./w:footerReference"):
            reference.getparent().remove(reference)
        extents = list(drawing.iter(word_drawing + "extent"))
        if extents:
            width, height = int(extents[0].get("cx")), int(extents[0].get("cy"))
            if not (0 < width <= 20_000_000 and 0 < height <= 20_000_000):
                raise ValueError("visual size limit")
            section.page_width = max(int(section.page_width), width + 1_828_800)
            section.page_height = max(int(section.page_height), height + 1_828_800)
        paragraph = document.add_paragraph()
        paragraph.add_run()._r.append(drawing)
        document.save(destination)
    elif extension == "xlsx":
        excel: Any = importlib.import_module("openpyxl")
        book = excel.load_workbook(source, keep_links=False)
        cached = excel.load_workbook(source, data_only=True, keep_links=False)
        try:
            if unit >= len(book.worksheets) or not book.worksheets[unit]._charts:
                raise ValueError("visual sheet not found")
            for index, sheet in enumerate(book.worksheets):
                sheet.sheet_state = "visible" if index == unit else "hidden"
                for row in sheet:
                    for cell in row:
                        if cell.data_type == "f":
                            cell.value = cached[sheet.title][cell.coordinate].value
                            if isinstance(cell.value, str):
                                cell.data_type = "s"
            sheet = book.worksheets[unit]
            book.active = unit
            sheet.sheet_properties.pageSetUpPr.fitToPage = True
            sheet.page_setup.fitToWidth = sheet.page_setup.fitToHeight = 1
            sheet.page_setup.orientation = "landscape"
            # Calc's used print area includes drawings, even beyond cells with values.
            sheet.print_area = ""
            sheet.print_title_rows = sheet.print_title_cols = None
            book.save(destination)
        finally:
            book.close()
            cached.close()
    else:
        raise ValueError("unsupported visual render copy")
