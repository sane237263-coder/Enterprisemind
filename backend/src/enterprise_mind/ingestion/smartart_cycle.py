"""Lower flat circular DiagramML to a bounded temporary DrawingML group.

Node diameter and ring radius follow the source aspect/spacing constraints.
Straight 2D connectors use the actual edge-to-edge gap and padding, rather
than a fraction of the rectangular slide frame. Native drawing caches are
handled by the caller and never supply computed coordinates to this module.
"""

import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from enterprise_mind.ingestion import smartart_linear as common

A, D, P = common.A, common.D, common.P
require, one, number, element = common.require, common.one, common.number, common.element


def _branches(node: Any, count: int) -> None:
    """Evaluate the supported normal-direction/count choices on a private tree."""
    for child in list(node):
        if child.tag != D + "choose":
            _branches(child, count)
            continue
        require(set(child.attrib) <= {"name"})
        require(len(child) == 2 and child[0].tag == D + "if" and child[1].tag == D + "else")
        condition, otherwise = child
        attributes = {k: v for k, v in condition.attrib.items() if k != "name"}
        require(set(otherwise.attrib) <= {"name"})
        if attributes == {"func": "var", "arg": "dir", "op": "equ", "val": "norm"}:
            accepted = True  # diagram_data rejects a reversed/non-default data variable.
        else:
            require(set(attributes) == {"axis", "ptType", "func", "op", "val"})
            require(attributes["func"] == "cnt")
            require(
                (attributes["axis"], attributes["ptType"])
                in {("ch", "node"), ("par ch", "doc node")}
            )
            value = number(attributes["val"], upper=64)
            require(value.is_integer() and attributes["op"] in {"gt", "lt"})
            accepted = count > value if attributes["op"] == "gt" else count < value
        branch = condition if accepted else otherwise
        _branches(branch, count)
        position = node.index(child)
        for selected in list(branch):
            node.insert(position, selected)
            position += 1
        node.remove(child)


@dataclass(frozen=True)
class CyclePlan:
    root: Any
    node: Any
    connector: Any
    angle: float
    gap: float
    connector_width: float
    connector_height: float
    begin_padding: float
    end_padding: float
    margins: tuple[float, ...]
    font_max: int
    font_min: int


def recognize(layout: Any, count: int) -> CyclePlan:
    require(3 <= count <= 64)
    root = deepcopy(one(layout, D + "layoutNode"))
    _branches(root, count)
    loop = one(root, D + "forEach")
    require(
        {k: v for k, v in loop.attrib.items() if k != "name"} == {"axis": "ch", "ptType": "node"}
    )
    node = one(loop, D + "layoutNode")
    transition = one(loop, D + "forEach")
    require(len(loop) == 2 and len(transition) == 1)
    require(
        {k: v for k, v in transition.attrib.items() if k != "name"}
        == {
            "axis": "followSib",
            "ptType": "sibTrans",
            "hideLastTrans": "0",
            "cnt": "1",
        }
    )
    connector = one(transition, D + "layoutNode")
    text = one(connector, D + "layoutNode")
    nodes = (root, node, connector, text)
    names = [n.get("name") for n in nodes]
    require(all(names) and len(set(names)) == 4 and len(list(root.iter(D + "layoutNode"))) == 4)
    for item in nodes:
        require(set(item.attrib) <= {"name", "styleLbl"})
        allowed = {
            D + tag
            for tag in (
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
        require(all(c.tag in allowed for c in item))
        for tag in ("alg", "shape", "presOf", "constrLst", "ruleLst"):
            one(item, D + tag)
    common._shape(root, "")
    common._shape(node, "ellipse")
    common._shape(connector, "conn")
    common._shape(text, "conn", hidden=True)
    algorithm = one(root, D + "alg")
    parameters = {p.get("type"): p.get("val") for p in algorithm}
    require(set(parameters) == {"stAng", "spanAng"} and parameters["spanAng"] == "360")
    start = number(parameters["stAng"], lower=-360, upper=360)
    common._algorithm(root, "cycle", {"stAng": parameters["stAng"], "spanAng": "360"})
    common._algorithm(node, "tx", {"txAnchorVertCh": "mid"})
    common._algorithm(connector, "conn", {"begPts": "auto", "endPts": "auto"})
    common._algorithm(text, "tx", {"autoTxRot": "grav"})
    require(not one(root, D + "presOf").attrib)
    require(dict(one(node, D + "presOf").attrib) == {"axis": "desOrSelf", "ptType": "node"})
    for item in (connector, text):
        require(dict(one(item, D + "presOf").attrib) == {"axis": "self"})

    constraints = common.Constraints(root)
    constraints.take("w", factor=1, **{"for": "ch", "ptType": "node", "refType": "w"})
    width = number(
        constraints.take(
            "w",
            factor=None,
            **{
                "for": "ch",
                "ptType": "sibTrans",
                "refType": "w",
                "refFor": "ch",
                "refPtType": "node",
            },
        ).get("fact", "1"),
        lower=0.01,
        upper=1,
    )
    gap = number(
        constraints.take("sibSp", factor=None, refType="w", refFor="ch", refPtType="node").get(
            "fact", "1"
        ),
        lower=0.01,
        upper=4,
    )
    font = next(
        (
            c
            for c in constraints.items
            if c.get("type") == "primFontSz" and c.get("ptType") == "node"
        ),
        None,
    )
    require(font is not None)
    assert font is not None
    maximum = number(font.get("val"), lower=1, upper=200)
    require(maximum.is_integer())
    constraints.take(
        "primFontSz", factor=1, value=font.get("val"), **{"for": "ch", "ptType": "node"}
    )
    text_font = next(
        (c for c in constraints.items if c.get("type") == "primFontSz" and c.get("val")), None
    )
    require(text_font is not None)
    assert text_font is not None
    number(text_font.get("val"), lower=1, upper=200)
    constraints.take(
        "primFontSz", value=text_font.get("val"), **{"for": "des", "forName": text.get("name")}
    )
    number(
        constraints.take(
            "primFontSz",
            factor=None,
            **{
                "for": "des",
                "forName": text.get("name"),
                "refType": "primFontSz",
                "refFor": "ch",
                "refPtType": "node",
                "op": "lte",
            },
        ).get("fact", "1"),
        lower=0.01,
        upper=1,
    )
    constraints.done()
    common.Constraints(root, "rule").done()

    constraints = common.Constraints(node)
    constraints.take("h", refType="w")
    margins = tuple(
        number(
            constraints.take(side + "Marg", factor=None, refType="primFontSz").get("fact", "1"),
            upper=1,
        )
        for side in "lrtb"
    )
    constraints.done()
    rules = common.Constraints(node, "rule")
    require(len(rules.items) == 1)
    minimum = number(rules.items[0].get("val"), lower=1, upper=maximum)
    require(minimum.is_integer())
    rules.take("primFontSz", value=str(int(minimum)))
    rules.done()

    constraints = common.Constraints(connector)
    height = number(
        constraints.take("h", factor=None, refType="w").get("fact", "1"), lower=0.01, upper=4
    )
    constraints.take("connDist")
    # The child is an empty connector label; consume and validate its constraints.
    number(
        constraints.take("w", factor=None, **{"for": "ch", "refType": "connDist"}).get("fact", "1"),
        upper=1,
    )
    constraints.take("h", factor=1, **{"for": "ch", "refType": "h"})
    pads = []
    for kind, default in (("begPad", 0.22), ("endPad", 0.25)):
        # Compatibility defaults for straight 2D cycle arrows. Independently
        # checked against native exports at multiple counts and frame sizes.
        padding = default
        if any(c.get("type") == kind for c in constraints.items):
            padding = number(
                constraints.take(kind, factor=None, refType="connDist").get("fact", "1"), upper=1
            )
        pads.append(padding)
    require(sum(pads) < 1)
    constraints.done()
    common.Constraints(connector, "rule").done()
    constraints = common.Constraints(text)
    for side in "lrtb":
        constraints.take(side + "Marg")
    constraints.done()
    rules = common.Constraints(text, "rule")
    rules.take("primFontSz", value=str(int(minimum)))
    rules.done()
    return CyclePlan(
        root,
        node,
        connector,
        start,
        gap,
        width,
        height,
        pads[0],
        pads[1],
        margins,
        int(maximum),
        int(minimum),
    )


def materialize(
    frame: Any, layout: Any, data: Any, colors: Any, style: Any, theme: Any, next_id: int
) -> tuple[Any, int]:
    count = sum(p.get("type", "node") == "node" for p in one(data, D + "ptLst"))
    plan = recognize(layout, count)
    groups, labels = common.diagram_data(data, plan.root, (plan.node, plan.connector))
    require(len(groups) == count and all(not group.children for group in groups))
    paints = [
        common._paint(labels[n.get("name")], colors, style, theme)
        for n in (plan.node, plan.connector)
    ]
    require(paints[0].family == paints[1].family)
    metrics = common.FontMetrics(paints[0].family)
    transform = one(frame, P + "xfrm")
    require(set(transform.attrib) <= {"rot", "flipH", "flipV"})
    extent = one(transform, A + "ext")
    width, height = (
        number(extent.get(k), lower=1, upper=100000000) / common.EMU for k in ("cx", "cy")
    )
    # Adjacent centers are one diameter plus the source sibling gap apart.
    radius_ratio = (1 + plan.gap) / (2 * math.sin(math.pi / count))
    angles = [math.radians(plan.angle) + index * 2 * math.pi / count for index in range(count)]
    xs, ys = (
        [radius_ratio * math.sin(a) for a in angles],
        [-radius_ratio * math.cos(a) for a in angles],
    )
    diameter = min(width / (max(xs) - min(xs) + 1), height / (max(ys) - min(ys) + 1))
    center_x = width / 2 - (max(xs) + min(xs)) * diameter / 2
    center_y = height / 2 - (max(ys) + min(ys)) * diameter / 2
    paragraphs = [common._paragraphs(group.parent) for group in groups]
    require(sum(len(common._text(p)) for group in paragraphs for p in group) <= 20000)
    size = None
    left, right, top, bottom = plan.margins
    for candidate in range(plan.font_max, plan.font_min - 1, -1):
        text_width = diameter / math.sqrt(2) - (left + right) * candidate
        text_height = diameter / math.sqrt(2) - (top + bottom) * candidate
        if text_width <= 0 or text_height <= 0:
            continue
        if all(
            sum(
                metrics.lines(common._text(p), text_width, candidate)
                * metrics.line_height
                * 0.9
                * candidate
                for p in group
            )
            + max(0, len(group) - 1) * 0.35 * candidate
            <= text_height
            for group in paragraphs
        ):
            size = candidate
            break
    require(size is not None)
    assert size is not None
    result = common.XML.Element(P + "grpSp")
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
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        index: int,
        arrow: bool = False,
        rotation: float = 0,
    ) -> None:
        nonlocal next_id
        require(all(math.isfinite(v) for v in (x, y, w, h)) and w > 0 and h > 0)
        # Check the actual rotated bounds before committing this diagram.
        angle = math.radians(rotation)
        half_x = (abs(w * math.cos(angle)) + abs(h * math.sin(angle))) / 2
        half_y = (abs(w * math.sin(angle)) + abs(h * math.cos(angle))) / 2
        require(x + w / 2 - half_x >= -0.01 and x + w / 2 + half_x <= width + 0.01)
        require(y + h / 2 - half_y >= -0.01 and y + h / 2 + half_y <= height + 0.01)
        shape = element(result, P + "sp")
        nv = element(shape, P + "nvSpPr")
        require(next_id < 2147483647)
        element(
            nv, P + "cNvPr", id=next_id, name=(plan.connector if arrow else plan.node).get("name")
        )
        next_id += 1
        element(nv, P + "cNvSpPr")
        element(nv, P + "nvPr")
        props = element(shape, P + "spPr")
        tr = element(props, A + "xfrm", rot=round(rotation * 60000))
        element(tr, A + "off", x=round(x * common.EMU), y=round(y * common.EMU))
        element(tr, A + "ext", cx=round(w * common.EMU), cy=round(h * common.EMU))
        adjustments = element(
            element(props, A + "prstGeom", prst="rightArrow" if arrow else "ellipse"), A + "avLst"
        )
        if arrow:
            element(adjustments, A + "gd", name="adj1", fmla="val 60000")
            element(adjustments, A + "gd", name="adj2", fmla="val 50000")
        paint = paints[int(arrow)]
        props.append(deepcopy(paint.fill))
        props.append(deepcopy(paint.line))
        if not arrow:
            common._text_body(
                shape, paragraphs[index], paint, size, bullet=False, bottom_margin=bottom
            )
            body = one(shape, P + "txBody/" + A + "bodyPr")
            body.set("anchor", "ctr")
            # LibreOffice's Calibri -> Carlito substitution places centered ink
            # about 3% of the font size below native Calibri. Shift the text
            # rectangle, retaining its total inset and fitted height. This is
            # checked against native single-line and wrapped-text exports.
            baseline = 0.03 if paint.family.casefold() == "calibri" else 0.0
            insets = (left, right, top - baseline, bottom + baseline)
            for key, margin in zip(("lIns", "rIns", "tIns", "bIns"), insets, strict=True):
                body.set(key, str(round(size * margin * common.EMU)))
            for paragraph in shape.iter(A + "pPr"):
                paragraph.set("algn", "ctr")

    radius = radius_ratio * diameter * math.cos(math.pi / count)
    gap = plan.gap * diameter
    connector_width = gap * (1 - plan.begin_padding - plan.end_padding)
    connector_height = plan.connector_width * diameter * plan.connector_height
    shift = gap * (plan.begin_padding - plan.end_padding) / 2
    for index, angle in enumerate(angles):
        draw(
            center_x + xs[index] * diameter - diameter / 2,
            center_y + ys[index] * diameter - diameter / 2,
            diameter,
            diameter,
            index=index,
        )
        angle += math.pi / count
        draw(
            center_x + radius * math.sin(angle) + shift * math.cos(angle) - connector_width / 2,
            center_y - radius * math.cos(angle) + shift * math.sin(angle) - connector_height / 2,
            connector_width,
            connector_height,
            index=index,
            arrow=True,
            rotation=math.degrees(angle) % 360,
        )
    return result, next_id
