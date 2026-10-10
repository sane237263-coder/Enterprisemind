"""Independently authored flat-cycle DiagramML for parser contracts."""

import io
from zipfile import ZipFile

from lxml import etree as E
from smartart_samples import A, D, linear_pptx_bytes


def cycle_pptx_bytes(count=5):
    def add(parent, tag, **attrs):
        return E.SubElement(parent, tag, **{key: str(value) for key, value in attrs.items()})

    def node(parent, name, algorithm, shape, presentation, parameters, hidden=False):
        item = add(parent, D + "layoutNode", name=name)
        alg = add(item, D + "alg", type=algorithm)
        for key, value in parameters.items():
            add(alg, D + "param", type=key, val=value)
        shape = add(item, D + "shape", type=shape)
        if hidden:
            shape.set("hideGeom", "1")
        add(shape, D + "adjLst")
        add(item, D + "presOf", **presentation)
        return item, add(item, D + "constrLst"), add(item, D + "ruleLst")

    def constraint(parent, kind, **attrs):
        return add(parent, D + "constr", type=kind, **attrs)

    layout = E.Element(D + "layoutDef")
    root, rc, _ = node(layout, "ring", "cycle", "", {}, {"stAng": "0", "spanAng": "360"})
    loop = add(root, D + "forEach", axis="ch", ptType="node")
    _, nc, nr = node(
        loop,
        "item",
        "tx",
        "ellipse",
        {"axis": "desOrSelf", "ptType": "node"},
        {"txAnchorVertCh": "mid"},
    )
    transition = add(
        loop, D + "forEach", axis="followSib", ptType="sibTrans", hideLastTrans="0", cnt=1
    )
    link, ac, _ = node(
        transition, "arrow", "conn", "conn", {"axis": "self"}, {"begPts": "auto", "endPts": "auto"}
    )
    _, tc, tr = node(link, "label", "tx", "conn", {"axis": "self"}, {"autoTxRot": "grav"}, True)
    constraint(rc, "w", **{"for": "ch", "ptType": "node", "refType": "w"})
    constraint(
        rc,
        "w",
        **{
            "for": "ch",
            "ptType": "sibTrans",
            "refType": "w",
            "refFor": "ch",
            "refPtType": "node",
            "fact": 0.25,
        },
    )
    constraint(rc, "sibSp", refType="w", refFor="ch", refPtType="node", fact=0.5)
    constraint(rc, "primFontSz", **{"for": "ch", "ptType": "node", "val": 65})
    constraint(rc, "primFontSz", **{"for": "des", "forName": "label", "val": 55})
    constraint(
        rc,
        "primFontSz",
        **{
            "for": "des",
            "forName": "label",
            "refType": "primFontSz",
            "refFor": "ch",
            "refPtType": "node",
            "op": "lte",
            "fact": 0.8,
        },
    )
    constraint(nc, "h", refType="w")
    for side in "lrtb":
        constraint(nc, side + "Marg", refType="primFontSz", fact=0.1)
        constraint(tc, side + "Marg")
    for rules in (nr, tr):
        add(rules, D + "rule", type="primFontSz", val=5, fact="NaN", max="NaN")
    constraint(ac, "h", refType="w", fact=1.35)
    constraint(ac, "connDist")
    constraint(ac, "w", **{"for": "ch", "refType": "connDist", "fact": 0.45})
    constraint(ac, "h", **{"for": "ch", "refType": "h"})

    data = E.Element(D + "dataModel")
    points, connections = add(data, D + "ptLst"), add(data, D + "cxnLst")
    add(points, D + "pt", modelId="root", type="doc")
    for index in range(count):
        point = add(points, D + "pt", modelId=str(index))
        text = add(point, D + "t")
        add(text, A + "bodyPr")
        add(text, A + "lstStyle")
        add(add(add(text, A + "p"), A + "r"), A + "t").text = chr(65 + index)
        add(
            connections,
            D + "cxn",
            modelId="edge" + str(index),
            srcId="root",
            destId=str(index),
            srcOrd=index,
        )
    for name, style in [("item", "ink"), ("arrow", "arrow")]:
        point = add(points, D + "pt", modelId="pres-" + name, type="pres")
        add(point, D + "prSet", presName=name, presStyleLbl=style)
    add(data, D + "bg")
    add(data, D + "whole")
    output = io.BytesIO()
    with ZipFile(io.BytesIO(linear_pptx_bytes())) as original, ZipFile(output, "w") as target:
        for item in original.infolist():
            contents = {
                "ppt/diagrams/lo.xml": E.tostring(layout),
                "ppt/diagrams/dm.xml": E.tostring(data),
            }.get(item.filename, original.read(item))
            target.writestr(item, contents)
    return output.getvalue()
