"""Resolve supported SmartArt constraints in a temporary PPTX render copy.

LibreOffice's linear diagram layout reads ``forName`` but ignores a constraint
selected only by ``ptType``. An ignored connector width can push shapes outside
the source graphic frame. Preserve the constraint and add its uniquely implied
layout name. Linear overlapping cards and flat cycles also need explicit
geometry/text styles; those are lowered from their constraints before import.
Neither path changes diagram data or the uploaded document.
"""

import importlib
import math
from pathlib import Path
from typing import Any
from zipfile import ZipFile

DGM = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}"
TRANSITIONS = {"sibTrans", "parTrans"}


def _child_layouts(layout: Any) -> list[tuple[Any, str | None]]:
    children: list[tuple[Any, str | None]] = []
    pending = [(child, None, True) for child in layout]
    while pending:
        element, point_type, unambiguous = pending.pop()
        if element.tag == DGM + "layoutNode":
            presentation = element.find(DGM + "presOf")
            if (
                not unambiguous
                or presentation is None
                or presentation.get("axis", "self") != "self"
                or presentation.get("ptType", point_type) != point_type
            ):
                point_type = None
            children.append((element, point_type))
        elif element.tag == DGM + "forEach":
            # Referenced loops, multi-axis selection and conditional branches do
            # not prove one name denotes the entire selected point type.
            point_type = element.get("ptType")
            unambiguous = (
                unambiguous
                and not element.get("ref")
                and element.get("axis") in {"ch", "followSib"}
                and point_type in {"node", "sibTrans", "parTrans"}
            )
            pending.extend((child, point_type, unambiguous) for child in element)
        elif element.tag in {DGM + "choose", DGM + "if", DGM + "else"}:
            pending.extend((child, None, False) for child in element)
    return children


def _explicit_transition_names(content: bytes) -> bytes:
    xml: Any = importlib.import_module("lxml.etree")
    root = xml.fromstring(
        content, parser=xml.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
    )
    if root.getroottree().docinfo.doctype:
        raise ValueError("DTD in SmartArt layout")
    if root.tag != DGM + "layoutDef":
        return content
    changed = False
    for layout in root.iter(DGM + "layoutNode"):
        algorithms = list(layout.findall(DGM + "alg"))
        for branch in layout.findall(DGM + "choose"):
            algorithms.extend(branch.findall("./*/" + DGM + "alg"))
        if not algorithms or any(algorithm.get("type") != "lin" for algorithm in algorithms):
            continue
        children = _child_layouts(layout)
        if any(kind is None for _, kind in children):
            continue
        names = [child.get("name") for child, _ in children]
        for constraint in layout.findall("./" + DGM + "constrLst/" + DGM + "constr"):
            point_type = constraint.get("ptType")
            if (
                point_type not in TRANSITIONS
                or constraint.get("for") != "ch"
                or constraint.get("forName")
                or constraint.get("type") not in {"w", "h"}
                or constraint.get("refType") != constraint.get("type")
                or constraint.get("refFor") != "ch"
                or constraint.get("refPtType", "all") != "all"
                or constraint.get("op", "equ") != "equ"
                or constraint.get("val", "0") != "0"
            ):
                continue
            targets = [child.get("name") for child, kind in children if kind == point_type]
            reference = constraint.get("refForName")
            if (
                len(targets) != 1
                or not targets[0]
                or names.count(targets[0]) != 1
                or not reference
                or names.count(reference) != 1
                or reference == targets[0]
            ):
                continue
            try:
                factor = float(constraint.get("fact", "1"))
            except ValueError:
                continue
            if not math.isfinite(factor) or not 0 < factor <= 1:
                continue
            constraint.set("forName", targets[0])
            changed = True
    return xml.tostring(root, xml_declaration=True, encoding="UTF-8") if changed else content


def smartart_render_copy(source: Path, destination: Path) -> bool:
    """Called only after package security validation, inside the bounded parser."""
    replacements: dict[str, bytes] = {}
    with ZipFile(source) as archive:
        from enterprise_mind.ingestion.smartart_linear import linear_render_parts

        replacements.update(linear_render_parts(archive))
        for entry in archive.infolist():
            if entry.filename.startswith("ppt/diagrams/") and entry.filename.endswith(".xml"):
                original = archive.read(entry)
                updated = _explicit_transition_names(original)
                if updated != original:
                    replacements[entry.filename] = updated
        if not replacements:
            return False
        with ZipFile(destination, "w") as output:
            for entry in archive.infolist():
                output.writestr(entry, replacements.get(entry.filename, archive.read(entry)))
    return True
