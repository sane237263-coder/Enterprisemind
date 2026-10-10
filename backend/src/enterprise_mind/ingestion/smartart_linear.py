"""Conservative lowering of linear, overlapping-card DiagramML to DrawingML.

LibreOffice does not implement this combination of font-relative dimensions,
local height bounds and named connector endpoints. The recognizer consumes the
complete supported layout grammar before emitting anything. Unrecognized
layouts, custom formatting, missing fonts and non-fitting text are left to the
original importer. No preset IDs, slide sizes or cached drawing coordinates are
used. This module only runs on the validated, temporary render copy.
"""

import importlib
import math
import posixpath
import struct
from copy import deepcopy
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from zipfile import ZipFile

D = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
MM = 72 / 25.4
EMU = 12700
XML: Any = importlib.import_module("lxml.etree")


class UnsupportedLayout(Exception):
    """Decline this frame atomically; retain the original importer behavior."""


def require(condition: bool) -> None:
    if not condition:
        raise UnsupportedLayout


def parse(content: bytes) -> Any:
    root = XML.fromstring(
        content, XML.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
    )
    if root.getroottree().docinfo.doctype:
        raise ValueError("DTD in SmartArt part")
    return root


def one(parent: Any, path: str) -> Any:
    items = parent.findall(path)
    require(len(items) == 1)
    return items[0]


def number(value: str | None, *, lower: float = 0, upper: float = 10000) -> float:
    try:
        result = float(value or "0")
    except ValueError:
        raise UnsupportedLayout from None
    require(math.isfinite(result) and lower <= result <= upper)
    return result


def element(parent: Any, tag: str, **attributes: object) -> Any:
    return XML.SubElement(parent, tag, **{key: str(value) for key, value in attributes.items()})


def selected(node: Any) -> list[Any]:
    """Only the normal-direction branch; the data model is checked separately."""
    result = []
    for child in node:
        if child.tag == D + "choose":
            require(len(child) == 2 and child[1].tag == D + "else")
            branch = child[0]
            require(branch.tag == D + "if")
            require(
                {k: v for k, v in branch.attrib.items() if k != "name"}
                == {"func": "var", "arg": "dir", "op": "equ", "val": "norm"}
            )
            require(all(c.tag in {D + "alg", D + "constrLst"} for c in branch))
            result.extend(branch)
        else:
            result.append(child)
    return result


class Constraints:
    """Every constraint must be consumed with its complete selection semantics."""

    def __init__(self, node: Any, kind: str = "constr") -> None:
        self.items = [c for block in selected(node) if block.tag == D + kind + "Lst" for c in block]
        self.kind = kind
        require(all(c.tag == D + kind and len(c) == 0 for c in self.items))

    def take(self, kind: str, *, factor: float | None = 1, value: str = "0", **keys: str) -> Any:
        defaults = {
            "for": "self",
            "refFor": "self",
            "ptType": "all",
            "refPtType": "all",
            "op": "equ",
        }
        selectors = {"type": kind, **keys}
        candidates = []
        for c in self.items:
            actual = {k: v for k, v in c.attrib.items() if k not in {"fact", "val", "max"}}
            actual = {k: v for k, v in actual.items() if defaults.get(k) != v}
            expected = {k: v for k, v in selectors.items() if defaults.get(k) != v}
            if actual == expected:
                candidates.append(c)
        require(len(candidates) == 1)
        result = candidates[0]
        if self.kind == "rule":
            require(result.get("fact", "NaN") == "NaN" and result.get("max", "NaN") == "NaN")
        elif factor is not None:
            require(number(result.get("fact", "1")) == factor)
        require(result.get("val", "0") == value)
        self.items.remove(result)
        return result

    def done(self) -> None:
        require(not self.items)


@dataclass(frozen=True)
class Plan:
    root: Any
    composite: Any
    title: Any
    background: Any
    body: Any
    connector: Any
    connector_text: Any
    gap: float
    width: float
    offset: float
    title_height: float
    background_height: float
    body_height: float
    title_cap: float
    bottom_margin: float
    arrow_height: float
    begin_pad: float
    end_pad: float
    font_max: int
    font_min: int


def _algorithm(node: Any, kind: str, parameters: dict[str, str]) -> None:
    algorithms = [c for c in selected(node) if c.tag == D + "alg"]
    require(len(algorithms) == 1)
    algorithm = algorithms[0]
    require(dict(algorithm.attrib) == {"type": kind})
    require(all(c.tag == D + "param" and set(c.attrib) == {"type", "val"} for c in algorithm))
    require(len(algorithm) == len(parameters))
    require({c.get("type"): c.get("val") for c in algorithm} == parameters)


def recognize(layout: Any) -> Plan:
    root = one(layout, D + "layoutNode")
    loop = one(root, D + "forEach")
    require(
        {k: v for k, v in loop.attrib.items() if k != "name"} == {"axis": "ch", "ptType": "node"}
    )
    composite = one(loop, D + "layoutNode")
    transition = one(loop, D + "forEach")
    require(len(loop) == 2 and len(transition) == 1)
    require(
        {k: v for k, v in transition.attrib.items() if k != "name"}
        == {"axis": "followSib", "ptType": "sibTrans", "cnt": "1"}
    )
    connector = one(transition, D + "layoutNode")
    nodes = composite.findall(D + "layoutNode")
    require(len(nodes) == 3)
    title, background, body = nodes
    connector_text = one(connector, D + "layoutNode")
    for node in (root, composite):
        _shape(node, "")
    _shape(connector_text, "conn", hidden=True)
    all_nodes = [root, composite, *nodes, connector, connector_text]
    names = [n.get("name") for n in all_nodes]
    require(all(names) and len(set(names)) == len(names))
    require(len(list(layout.iter(D + "layoutNode"))) == len(all_nodes))
    for n in all_nodes:
        require(set(n.attrib) <= {"name", "styleLbl"})
        require(
            all(
                c.tag
                in {
                    D + k
                    for k in (
                        "varLst",
                        "alg",
                        "shape",
                        "presOf",
                        "constrLst",
                        "ruleLst",
                        "forEach",
                        "layoutNode",
                    )
                }
                for c in selected(n)
            )
        )
    _algorithm(root, "lin", {})
    _algorithm(composite, "composite", {})
    _algorithm(title, "tx", {"parTxLTRAlign": "l", "parTxRTLAlign": "r", "txAnchorVert": "t"})
    _algorithm(background, "sp", {})
    _algorithm(body, "tx", {"stBulletLvl": "1"})
    _algorithm(
        connector,
        "conn",
        {
            "begPts": "auto",
            "endPts": "auto",
            "srcNode": title.get("name"),
            "dstNode": title.get("name"),
        },
    )
    _algorithm(connector_text, "tx", {"autoTxRot": "grav"})
    for n, axis in [(title, "self"), (background, "self"), (body, "des")]:
        require(dict(one(n, D + "presOf").attrib) == {"axis": axis, "ptType": "node"})
    for n in (root, composite):
        require(not one(n, D + "presOf").attrib)
    for n in (connector, connector_text):
        require(dict(one(n, D + "presOf").attrib) == {"axis": "self"})
    cn, tn, bn, dn, an, atn = [
        n.get("name") for n in (composite, title, background, body, connector, connector_text)
    ]
    c = Constraints(root)
    c.take("w", **{"for": "ch", "forName": cn, "refType": "w"})
    # Accept both original point-type selection and r48's explicit-name repair.
    transition_constraint: Any = next((x for x in c.items if x.get("ptType") == "sibTrans"), None)
    require(transition_constraint is not None)
    extra = {"forName": an} if transition_constraint.get("forName") else {}
    gap = number(
        c.take(
            "w",
            factor=None,
            **{
                "for": "ch",
                "ptType": "sibTrans",
                "refType": "w",
                "refFor": "ch",
                "refForName": cn,
                **extra,
            },
        ).get("fact", "1"),
        lower=0.001,
        upper=1,
    )
    for n in (tn, bn, dn):
        for dimension in ("w", "h"):
            c.take(dimension, **{"for": "des", "forName": n})
    font_constraint: Any = next(
        (x for x in c.items if x.get("type") == "primFontSz" and x.get("forName") == tn), None
    )
    require(font_constraint is not None)
    font_max = number(font_constraint.get("val"), lower=1, upper=200)
    require(font_max.is_integer())
    c.take("primFontSz", value=font_constraint.get("val"), **{"for": "des", "forName": tn})
    c.take(
        "secFontSz",
        **{"for": "des", "forName": dn, "refType": "primFontSz", "refFor": "des", "refForName": tn},
    )
    for op in ("equ", "lte"):
        c.take(
            "primFontSz",
            factor=None,
            **{
                "for": "des",
                "forName": atn,
                "refType": "primFontSz",
                "refFor": "des",
                "refForName": tn,
                "op": op,
            },
        )
    heights = [
        number(
            c.take(
                "h",
                factor=None,
                **{
                    "for": "des",
                    "forName": n,
                    "refType": "primFontSz",
                    "refFor": "des",
                    "refForName": tn,
                },
            ).get("fact", "1"),
            lower=0.01,
            upper=10,
        )
        for n in (tn, bn, dn)
    ]
    # These bounds operate in the ancestor's scope, before the child's local cap.
    # Require them to agree; contradictory or different bounds are not ignored.
    for op in ("lte", "gte"):
        bound = number(
            c.take(
                "h",
                factor=None,
                **{
                    "for": "des",
                    "forName": bn,
                    "refType": "h",
                    "refFor": "des",
                    "refForName": tn,
                    "op": op,
                },
            ).get("fact", "1")
        )
        require(math.isclose(heights[1], heights[0] * bound, abs_tol=1e-9))
    c.done()
    rules = Constraints(root, "rule")
    rules.take("w", **{"for": "ch", "forName": cn})
    minimum: Any = next((r for r in rules.items if r.get("type") == "primFontSz"), None)
    require(minimum is not None)
    font_min = number(minimum.get("val"), lower=1, upper=font_max)
    require(font_min.is_integer())
    rules.take("primFontSz", value=minimum.get("val"), **{"for": "des", "forName": tn})
    rules.done()
    c = Constraints(composite)
    # Infinite grow allowance, represented by the layout's large aspect ratio.
    grow = number(c.take("h", factor=None, refType="w").get("fact", "1"))
    require(grow >= 100)
    for n in (tn, bn):
        c.take("l", **{"for": "ch", "forName": n})
        c.take("t", **{"for": "ch", "forName": n})
    width = number(
        c.take("w", factor=None, **{"for": "ch", "forName": tn, "refType": "w"}).get("fact", "1"),
        lower=0.01,
        upper=1,
    )
    offset = number(
        c.take("l", factor=None, **{"for": "ch", "forName": dn, "refType": "w"}).get("fact", "1"),
        upper=1,
    )
    require(width + offset <= 1.000001)
    for n in (bn, dn):
        c.take("w", **{"for": "ch", "forName": n, "refType": "w", "refFor": "ch", "refForName": tn})
    c.take("t", **{"for": "ch", "forName": dn, "refType": "h", "refFor": "ch", "refForName": tn})
    c.done()
    for n in (composite, title, body):
        rules = Constraints(n, "rule")
        rules.take("h", value="INF")
        rules.done()
    Constraints(background, "rule").done()
    Constraints(connector, "rule").done()
    c = Constraints(title)
    cap = number(
        c.take("h", factor=None, refType="w", op="lte").get("fact", "1"), lower=0.01, upper=10
    )
    bottom_margin = number(
        c.take("bMarg", factor=None, refType="primFontSz").get("fact", "1"), upper=5
    )
    c.take("h")
    c.done()
    c = Constraints(background)
    c.take("h")
    c.done()
    c = Constraints(body)
    c.take("secFontSz", value=font_constraint.get("val"))
    c.take("primFontSz", refType="secFontSz")
    c.take("h")
    c.done()
    c = Constraints(connector)
    arrow_height = number(
        c.take("h", factor=None, refType="w").get("fact", "1"), lower=0.01, upper=5
    )
    c.take("connDist")
    pads = [
        number(c.take(p, factor=None, refType="connDist").get("fact", "1"), upper=1)
        for p in ("begPad", "endPad")
    ]
    require(sum(pads) < 1)
    c.done()
    c = Constraints(connector_text)
    for side in "lrtb":
        c.take(side + "Marg")
    c.done()
    rules = Constraints(connector_text, "rule")
    rules.take("primFontSz", value=str(int(font_min)))
    rules.done()
    return Plan(
        root,
        composite,
        title,
        background,
        body,
        connector,
        connector_text,
        gap,
        width,
        offset,
        heights[0],
        heights[1],
        heights[2],
        cap,
        bottom_margin,
        arrow_height,
        pads[0],
        pads[1],
        int(font_max),
        int(font_min),
    )


def _shape(node: Any, expected: str | None, *, hidden: bool = False) -> tuple[str, float]:
    shape = one(node, D + "shape")
    require(set(shape.attrib) <= {"type", R + "blip", "hideGeom", "zOrderOff"})
    require(not shape.get(R + "blip"))
    require(shape.get("hideGeom", "0") in ({"1", "true"} if hidden else {"0", "false"}))
    require(shape.get("zOrderOff", "0") in ({"0", "1"} if hidden else {"0"}))
    kind = shape.get("type", "")
    require(kind == expected if expected is not None else kind in {"rect", "roundRect"})
    adjustments = one(shape, D + "adjLst")
    require(len(shape) == 1)
    if len(adjustments):
        require(len(adjustments) == 1 and kind in {"rect", "roundRect"})
        adj = adjustments[0]
        require(
            adj.tag == D + "adj" and set(adj.attrib) == {"idx", "val"} and adj.get("idx") == "1"
        )
        radius = number(adj.get("val"), upper=0.5)
    else:
        radius = 1 / 6 if kind == "roundRect" else 0
    return str(kind), radius


def _variables(parent: Any) -> None:
    defaults = {
        "dir": "norm",
        "animLvl": "lvl",
        "resizeHandles": "exact",
        "chMax": "0",
        "chPref": "0",
        "bulletEnabled": "1",
    }
    for tag in (D + "varLst", D + "presLayoutVars"):
        for block in parent.iter(tag):
            require(not block.attrib)
            for var in block:
                key = XML.QName(var).localname
                require(var.tag == D + key and key in defaults and set(var.attrib) <= {"val"})
                require(len(var) == 0)
                require(var.get("val", defaults[key]) == defaults[key])


@dataclass(frozen=True)
class TextGroup:
    parent: Any
    children: tuple[Any, ...]


def diagram_data(
    data: Any, root: Any, style_nodes: tuple[Any, ...]
) -> tuple[list[TextGroup], dict[str, str]]:
    _variables(data)
    _variables(root)
    points = list(one(data, D + "ptLst"))
    require(0 < len(points) <= 2048)
    ids = {p.get("modelId"): p for p in points}
    require(None not in ids and len(ids) == len(points))
    roots = [p for p in points if p.get("type") == "doc"]
    require(len(roots) == 1)
    require(
        all(
            p.tag == D + "pt"
            and p.get("type", "node") in {"node", "doc", "pres", "sibTrans", "parTrans"}
            for p in points
        )
    )
    allowed_properties = {
        "loTypeId",
        "loCatId",
        "qsTypeId",
        "qsCatId",
        "csTypeId",
        "csCatId",
        "phldr",
        "phldrT",
        "presAssocID",
        "presName",
        "presStyleLbl",
        "presStyleIdx",
        "presStyleCnt",
    }
    for point in points:
        require(set(point.attrib) <= {"modelId", "type", "cxnId"})
        require(all(c.tag in {D + "prSet", D + "spPr", D + "t"} for c in point))
        require(all(not c.attrib and len(c) == 0 for c in point.findall(D + "spPr")))
        for props in point.findall(D + "prSet"):
            require(set(props.attrib) <= allowed_properties)
            require(all(c.tag == D + "presLayoutVars" for c in props))
        if point.get("type", "node") != "node":
            require(not any(t.text for t in point.iter(A + "t")))
    require(
        all(
            len(c) == 0 and not c.attrib
            for c in list(data.findall(D + "bg")) + list(data.findall(D + "whole"))
        )
    )
    connections = list(one(data, D + "cxnLst"))
    require(len(connections) <= 4096)
    hierarchy: dict[str, list[tuple[int, Any]]] = {}
    seen_children: set[str] = set()
    for connection in connections:
        require(connection.tag == D + "cxn")
        if connection.get("type", "parOf") != "parOf":
            require(connection.get("type") in {"presOf", "presParOf"})
            continue
        source, target = connection.get("srcId"), connection.get("destId")
        require(source in ids and target in ids and target not in seen_children)
        require(ids[target].get("type", "node") == "node")
        seen_children.add(target)
        order = number(connection.get("srcOrd"), upper=2048)
        require(order.is_integer())
        hierarchy.setdefault(source, []).append((int(order), ids[target]))

    def children(model_id: str) -> list[Any]:
        found = hierarchy.get(model_id, [])
        require(len({index for index, _ in found}) == len(found))
        return [point for _, point in sorted(found, key=lambda row: row[0])]

    parents = children(roots[0].get("modelId"))
    require(1 <= len(parents) <= 64)
    groups = [TextGroup(p, tuple(children(p.get("modelId")))) for p in parents]
    require(all(not children(c.get("modelId")) for g in groups for c in g.children))
    visited = {p.get("modelId") for g in groups for p in (g.parent, *g.children)}
    require(visited == {p.get("modelId") for p in points if p.get("type", "node") == "node"})
    styles: dict[str, str] = {}
    for node in style_nodes:
        labels = {
            props.get("presStyleLbl")
            for p in points
            if p.get("type") == "pres"
            for props in p.findall(D + "prSet")
            if props.get("presName") == node.get("name")
        }
        require(len(labels) == 1 and None not in labels)
        label = str(next(iter(labels)))
        require(not node.get("styleLbl") or node.get("styleLbl") == label)
        styles[node.get("name")] = label
    return groups, styles


class FontMetrics:
    def __init__(self, family: str) -> None:
        require(family.casefold() in {"calibri", "carlito"})
        paths = [Path("/usr/share/fonts/truetype/crosextra/Carlito-Regular.ttf")]
        paths.extend(Path("/opt").glob("libreoffice*/share/fonts/truetype/Carlito-Regular.ttf"))
        paths.append(Path("C:/Windows/Fonts/calibri.ttf"))
        found = next((p for p in paths if p.is_file()), None)
        if found is None:
            raise UnsupportedLayout
        self.font: Any = importlib.import_module("pymupdf").Font(fontfile=str(found))
        self.font_path = found
        self.line_cache: dict[tuple[str, float, float], int] = {}
        content = found.read_bytes()
        require(len(content) >= 12)
        count = struct.unpack_from(">H", content, 4)[0]
        require(count <= 256 and len(content) >= 12 + count * 16)
        tables = {}
        for i in range(count):
            tag, _, offset, length = struct.unpack_from(">4sIII", content, 12 + i * 16)
            require(offset + length <= len(content))
            tables[tag] = content[offset : offset + length]
        require(len(tables.get(b"head", b"")) >= 20 and len(tables.get(b"OS/2", b"")) >= 78)
        units = struct.unpack_from(">H", tables[b"head"], 18)[0]
        ascent, descent = struct.unpack_from(">HH", tables[b"OS/2"], 74)
        require(units > 0)
        self.line_height: float = (ascent + descent) / units
        require(0.5 <= self.line_height <= 3)

    def lines(self, text: str, width: float, size: float) -> int:
        require(width > 0 and len(text) <= 20000)
        require(all(self.font.has_glyph(ord(c)) for c in text if c not in "\n\r\t"))
        key = (text, width, size)
        if key not in self.line_cache:
            pdf: Any = importlib.import_module("pymupdf")
            # Story uses HarfBuzz, including pair kerning. Font.text_length()
            # sums unshaped advances and can incorrectly shrink a fitting line.
            css = (
                '@font-face{font-family:measure;src:url("'
                + self.font_path.name
                + '")}body{margin:0}p{margin:0;font-family:measure;'
                + f"font-size:{size}pt;line-height:1;white-space:pre-wrap}}"
            )
            story = pdf.Story(
                "<p>" + escape(text or " ") + "</p>",
                user_css=css,
                archive=pdf.Archive(str(self.font_path.parent)),
            )
            more, filled = story.place(pdf.Rect(0, 0, width, size * 20001))
            # Story may expand an unbreakable word beyond the supplied width.
            # Treat that as a failed fit, including a single oversized glyph.
            self.line_cache[key] = (
                20001 if more or filled[2] > width + 0.01 else max(1, round(filled[3] / size))
            )
        return self.line_cache[key]


def _paragraphs(point: Any) -> list[Any]:
    text = one(point, D + "t")
    require(all(c.tag in {A + "bodyPr", A + "lstStyle", A + "p"} for c in text))
    for tag in ("bodyPr", "lstStyle"):
        empty = one(text, A + tag)
        require(not empty.attrib and len(empty) == 0)
    paragraphs = text.findall(A + "p")
    require(0 < len(paragraphs) <= 128)
    for paragraph in paragraphs:
        require(not paragraph.attrib)
        for item in paragraph:
            require(item.tag in {A + "r", A + "endParaRPr"})
            if item.tag == A + "r":
                require(not item.attrib and len(item.findall(A + "t")) == 1)
                require(all(c.tag in {A + "rPr", A + "t"} for c in item))
                properties = item.findall(A + "rPr")
            else:
                properties = [item]
            for props in properties:
                require(set(props.attrib) <= {"lang", "dirty", "smtClean"} and len(props) == 0)
    return list(paragraphs)


def _text(paragraph: Any) -> str:
    return "".join(t.text or "" for t in paragraph.iter(A + "t"))


def relationships(
    archive: ZipFile, part: str, *, optional: bool = False
) -> dict[str, tuple[str, str]]:
    directory, name = posixpath.split(part)
    path = posixpath.join(directory, "_rels", name + ".rels")
    if optional and path not in archive.namelist():
        return {}
    require(path in archive.namelist())
    result = {}
    for rel in parse(archive.read(path)):
        require(rel.get("TargetMode", "Internal") == "Internal")
        target = rel.get("Target", "")
        require(bool(target) and "\\" not in target and ":" not in target)
        resolved = posixpath.normpath(posixpath.join(directory, target))
        require(not resolved.startswith(("/", "../")) and resolved in archive.namelist())
        require(rel.get("Id") not in result)
        result[rel.get("Id")] = (resolved, rel.get("Type", "").rsplit("/", 1)[-1])
    return result


def _theme(archive: ZipFile, slide: str) -> Any:
    current = slide
    for kind in ("slideLayout", "slideMaster", "theme"):
        matches = [
            path for path, relation in relationships(archive, current).values() if relation == kind
        ]
        require(len(matches) == 1)
        current = matches[0]
    return parse(archive.read(current))


@dataclass(frozen=True)
class Paint:
    fill: Any
    line: Any
    text: Any
    family: str


def _paint(label: str, colors: Any, style: Any, theme: Any) -> Paint:
    labels = [n for n in colors.findall(D + "styleLbl") if n.get("name") == label]
    presets = [n for n in style.findall(D + "styleLbl") if n.get("name") == label]
    require(len(labels) == len(presets) == 1)
    palette, preset = labels[0], presets[0]
    for tag in ("sp3d", "txPr"):
        require(all(not e.attrib and len(e) == 0 for e in preset.findall(D + tag)))
    for camera in preset.iter(A + "camera"):
        require(dict(camera.attrib) == {"prst": "orthographicFront"} and len(camera) == 0)
    references = one(preset, D + "style")
    require(one(references, A + "effectRef").get("idx") == "0")
    font_ref = one(references, A + "fontRef")
    require(font_ref.get("idx") in {"minor", "major"} and len(font_ref) <= 1)
    font_scheme = one(theme, "./" + A + "themeElements/" + A + "fontScheme")
    family = one(font_scheme, "./" + A + str(font_ref.get("idx")) + "Font/" + A + "latin").get(
        "typeface"
    )
    require(bool(family))

    def color(kind: str, fallback: Any = None) -> Any:
        listing = one(palette, D + kind + "ClrLst")
        require(listing.get("meth", "span") in {"repeat", "span"})
        require(len(listing) <= 1)
        chosen = listing[0] if len(listing) else fallback
        require(chosen is not None)
        require(chosen.tag in {A + "schemeClr", A + "srgbClr", A + "scrgbClr", A + "sysClr"})
        return deepcopy(chosen)

    fill_color = color("fill")
    line_color = color("lin")
    text_color = color("txFill", font_ref[0] if len(font_ref) else None)
    # Text outlines and effects are not flattened into an approximation.
    for kind in ("txLin", "txEffect", "effect"):
        require(len(one(palette, D + kind + "ClrLst")) == 0)
    format_scheme = one(theme, "./" + A + "themeElements/" + A + "fmtScheme")
    fills = one(format_scheme, A + "fillStyleLst")
    index = int(number(one(references, A + "fillRef").get("idx"), lower=1, upper=len(fills))) - 1
    fill = fills[index]
    require(fill.tag == A + "solidFill" and len(fill) == 1)
    require(fill[0].tag == A + "schemeClr" and fill[0].get("val") == "phClr" and len(fill[0]) == 0)
    result_fill = XML.Element(A + "solidFill")
    result_fill.append(fill_color)
    lines = one(format_scheme, A + "lnStyleLst")
    index = int(number(one(references, A + "lnRef").get("idx"), upper=len(lines)))
    if index == 0:
        line = XML.Element(A + "ln")
        element(line, A + "noFill")
    else:
        line = deepcopy(lines[index - 1])
        solid = one(line, A + "solidFill")
        require(
            len(solid) == 1 and solid[0].tag == A + "schemeClr" and solid[0].get("val") == "phClr"
        )
        # Retain any line-style color transforms after the palette's transforms.
        replacement = deepcopy(line_color)
        replacement.extend(deepcopy(c) for c in solid[0])
        solid.replace(solid[0], replacement)
    return Paint(result_fill, line, text_color, str(family))


@dataclass(frozen=True)
class Geometry:
    size: int
    column: float
    width: float
    title_height: float
    background_height: float
    body_height: float
    top: float
    title_radius: float
    body_radius: float


def _fit(
    plan: Plan, groups: list[TextGroup], width: float, height: float, metrics: FontMetrics
) -> Geometry:
    column = width / (len(groups) + (len(groups) - 1) * plan.gap)
    box_width = column * plan.width
    title_kind, title_radius = _shape(plan.title, "rect", hidden=True)
    _shape(plan.background, None)
    body_kind, body_radius = _shape(plan.body, None)
    del title_kind
    parent_paragraphs = [_paragraphs(g.parent) for g in groups]
    child_paragraphs = [[p for point in g.children for p in _paragraphs(point)] for g in groups]
    require(
        sum(len(_text(p)) for batch in (*parent_paragraphs, *child_paragraphs) for p in batch)
        <= 20000
    )

    def needed(paragraphs: list[Any], size: int, box_height: float, *, body: bool) -> float:
        inset = 0.0
        if body and body_kind == "roundRect":
            inset = min(box_width, box_height) * body_radius * (1 - math.sqrt(0.5))
        indent = size * 2 / 3 if body else 0
        available = box_width - size * 1.12 - 2 * inset - indent
        if available <= 0:
            return math.inf
        line_count = sum(metrics.lines(_text(p), available, size) for p in paragraphs)
        spacing = max(0, len(paragraphs) - 1) * (0.15 if body else 0.35) * metrics.line_height
        # DiagramML text uses 90% of the font's Windows line metrics; default
        # insets are .56em. Margin/font references are points, not millimetres.
        vertical = size * (0.56 + (0.56 if body else plan.bottom_margin))
        return (
            size * (line_count * metrics.line_height * 0.9 + spacing + 0.00695)
            + vertical
            + 2 * inset
        )

    for size in range(plan.font_max, plan.font_min - 1, -1):
        th = min(size * plan.title_height * MM, box_width * plan.title_cap)
        bh = size * plan.background_height * MM
        dh = size * plan.body_height * MM
        # The local h=INF rule permits body text growth; retain one common size
        # for all instances of the same layout node, including unequal contents.
        required = max(needed(batch, size, dh, body=True) for batch in child_paragraphs)
        if required > dh:
            dh = required
        if not math.isfinite(dh):
            continue
        if any(needed(batch, size, th, body=False) > th for batch in parent_paragraphs):
            continue
        if th + dh > height or bh > height:
            continue
        if any(needed(batch, size, dh, body=True) > dh + 0.01 for batch in child_paragraphs):
            continue
        return Geometry(
            size,
            column,
            box_width,
            th,
            bh,
            dh,
            (height - max(bh, th + dh)) / 2,
            title_radius,
            body_radius,
        )
    raise UnsupportedLayout


def _text_body(
    shape: Any,
    paragraphs: list[Any],
    paint: Paint,
    size: int,
    *,
    bullet: bool,
    bottom_margin: float,
) -> None:
    if not paragraphs:
        return
    body = element(shape, P + "txBody")
    properties = element(
        body,
        A + "bodyPr",
        anchor="t",
        lIns=round(size * 0.56 * EMU),
        rIns=round(size * 0.56 * EMU),
        tIns=round(size * 0.56 * EMU),
        bIns=round(size * bottom_margin * EMU),
    )
    element(properties, A + "noAutofit")
    element(body, A + "lstStyle")
    for original in paragraphs:
        paragraph = element(body, A + "p")
        indent = round(size * 2 / 3 * EMU) if bullet else 0
        props = element(paragraph, A + "pPr", algn="l", marL=indent, indent=-indent)
        element(element(props, A + "lnSpc"), A + "spcPct", val=90000)
        element(element(props, A + "spcAft"), A + "spcPct", val=15000 if bullet else 35000)
        if bullet:
            element(props, A + "buFont", typeface=paint.family)
            element(props, A + "buChar", char="•")
        else:
            element(props, A + "buNone")
        for run in original.findall(A + "r"):
            copied = deepcopy(run)
            properties = copied.find(A + "rPr")
            if properties is None:
                properties = XML.Element(A + "rPr")
                copied.insert(0, properties)
            properties.set("sz", str(size * 100))
            element(properties, A + "solidFill").append(deepcopy(paint.text))
            element(properties, A + "latin", typeface=paint.family)
            paragraph.append(copied)
        element(paragraph, A + "endParaRPr", sz=size * 100)


def materialize(
    frame: Any, plan: Plan, data: Any, colors: Any, style: Any, theme: Any, next_id: int
) -> tuple[Any, int]:
    for node in (plan.root, plan.composite):
        _shape(node, "")
    _shape(plan.connector, "conn")
    _shape(plan.connector_text, "conn", hidden=True)
    groups, labels = diagram_data(
        data, plan.root, (plan.title, plan.background, plan.body, plan.connector)
    )
    paints = {
        node.get("name"): _paint(labels[node.get("name")], colors, style, theme)
        for node in (plan.title, plan.background, plan.body, plan.connector)
    }
    families = {paint.family for paint in paints.values()}
    require(len(families) == 1)
    metrics = FontMetrics(next(iter(families)))
    transform = one(frame, P + "xfrm")
    require(set(transform.attrib) <= {"rot", "flipH", "flipV"})
    extent = one(transform, A + "ext")
    width = number(extent.get("cx"), lower=1, upper=100000000) / EMU
    height = number(extent.get("cy"), lower=1, upper=100000000) / EMU
    geometry = _fit(plan, groups, width, height, metrics)
    result = XML.Element(P + "grpSp")
    nonvisual = element(result, P + "nvGrpSpPr")
    nonvisual.append(deepcopy(one(frame, "./" + P + "nvGraphicFramePr/" + P + "cNvPr")))
    element(nonvisual, P + "cNvGrpSpPr")
    nonvisual.append(deepcopy(one(frame, "./" + P + "nvGraphicFramePr/" + P + "nvPr")))
    group_transform = deepcopy(transform)
    group_transform.tag = A + "xfrm"
    element(group_transform, A + "chOff", x=0, y=0)
    element(group_transform, A + "chExt", cx=extent.get("cx"), cy=extent.get("cy"))
    element(result, P + "grpSpPr").append(group_transform)

    def draw(
        node: Any,
        bounds: tuple[float, float, float, float],
        paragraphs: list[Any],
        *,
        hidden: bool = False,
        bullet: bool = False,
        arrow: bool = False,
    ) -> None:
        nonlocal next_id
        x, y, w, h = bounds
        require(all(math.isfinite(v) for v in bounds) and w > 0 and h > 0)
        require(x >= -0.01 and y >= -0.01 and x + w <= width + 0.01 and y + h <= height + 0.01)
        shape = element(result, P + "sp")
        nv = element(shape, P + "nvSpPr")
        require(next_id < 2147483647)
        element(nv, P + "cNvPr", id=next_id, name=node.get("name"))
        next_id += 1
        element(nv, P + "cNvSpPr")
        element(nv, P + "nvPr")
        props = element(shape, P + "spPr")
        tr = element(props, A + "xfrm")
        element(tr, A + "off", x=round(x * EMU), y=round(y * EMU))
        element(tr, A + "ext", cx=round(w * EMU), cy=round(h * EMU))
        kind, radius = _shape(node, "conn" if arrow else None, hidden=hidden)
        preset = element(props, A + "prstGeom", prst="rightArrow" if arrow else kind)
        adjustments = element(preset, A + "avLst")
        if arrow:
            # DiagramML's 2D straight connector has a 60% shaft and half-height head.
            element(adjustments, A + "gd", name="adj1", fmla="val 60000")
            element(adjustments, A + "gd", name="adj2", fmla="val 50000")
        elif kind == "roundRect":
            element(adjustments, A + "gd", name="adj", fmla=f"val {round(radius * 100000)}")
        paint = paints[node.get("name")]
        if hidden:
            element(props, A + "noFill")
            element(element(props, A + "ln"), A + "noFill")
        else:
            props.append(deepcopy(paint.fill))
            props.append(deepcopy(paint.line))
        _text_body(
            shape,
            paragraphs,
            paint,
            geometry.size,
            bullet=bullet,
            bottom_margin=0.56 if bullet else plan.bottom_margin,
        )

    g = geometry
    for index, group in enumerate(groups):
        x = index * g.column * (1 + plan.gap)
        draw(plan.background, (x, g.top, g.width, g.background_height), [])
        draw(
            plan.body,
            (x + g.column * plan.offset, g.top + g.title_height, g.width, g.body_height),
            [p for c in group.children for p in _paragraphs(c)],
            bullet=True,
        )
        draw(
            plan.title, (x, g.top, g.width, g.title_height), _paragraphs(group.parent), hidden=True
        )
        if index < len(groups) - 1:
            begin = x + g.width
            end = x + g.column * (1 + plan.gap)
            distance = end - begin
            arrow_height = g.column * plan.gap * plan.arrow_height
            draw(
                plan.connector,
                (
                    begin + distance * plan.begin_pad,
                    g.top + g.title_height / 2 - arrow_height / 2,
                    distance * (1 - plan.begin_pad - plan.end_pad),
                    arrow_height,
                ),
                [],
                arrow=True,
            )
    return result, next_id


def linear_render_parts(archive: ZipFile) -> dict[str, bytes]:
    replacements = {}
    for part in archive.namelist():
        if (
            not part.startswith("ppt/slides/slide")
            or not part.endswith(".xml")
            or "/_rels/" in part
        ):
            continue
        slide = parse(archive.read(part))
        frames = slide.findall(".//" + P + "graphicFrame")
        changed = False
        next_id = max((int(node.get("id", "0")) for node in slide.iter(P + "cNvPr")), default=0) + 1
        for frame in frames:
            ids = frame.find(".//" + D + "relIds")
            if ids is None:
                continue
            try:
                relations = relationships(archive, part)
                paths = {
                    key: relations[ids.get(R + key)][0]
                    for key in ("lo", "dm", "qs", "cs")
                    if ids.get(R + key) in relations
                }
                require(len(paths) == 4)
                data = parse(archive.read(paths["dm"]))
                # Existing native drawing caches take precedence; their custom
                # coordinates and formatting must not be recalculated.
                drawing_links = list(relations.values()) + list(
                    relationships(archive, paths["dm"], optional=True).values()
                )
                for path, kind in drawing_links:
                    if kind == "diagramDrawing":
                        cached = parse(archive.read(path))
                        require(
                            not any(
                                XML.QName(n).localname in {"sp", "pic", "cxnSp", "graphicFrame"}
                                for n in cached.iter()
                            )
                        )
                layout = parse(archive.read(paths["lo"]))
                colors = parse(archive.read(paths["cs"]))
                style = parse(archive.read(paths["qs"]))
                theme = _theme(archive, part)
                try:
                    plan = recognize(layout)
                except UnsupportedLayout:
                    from enterprise_mind.ingestion.smartart_cycle import (
                        materialize as materialize_cycle,
                    )

                    group, allocated = materialize_cycle(
                        frame, layout, data, colors, style, theme, next_id
                    )
                else:
                    group, allocated = materialize(frame, plan, data, colors, style, theme, next_id)
            except UnsupportedLayout:
                continue
            frame.getparent().replace(frame, group)
            next_id = allocated
            changed = True
        if changed:
            replacements[part] = XML.tostring(slide, xml_declaration=True, encoding="UTF-8")
    return replacements
