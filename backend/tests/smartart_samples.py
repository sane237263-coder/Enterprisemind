"""Small, independently authored DiagramML package for compatibility contracts."""

import io
from zipfile import ZipFile

from lxml import etree as E
from media_samples import visual_pptx_bytes

D = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def linear_pptx_bytes():
    def add(parent, tag, **attrs):
        return E.SubElement(parent, tag, **{k: str(v) for k, v in attrs.items()})

    def node(parent, name, alg, shape="", *, hidden=False, axis=None, style=None, params=None):
        n = add(parent, D + "layoutNode", name=name)
        if style:
            n.set("styleLbl", style)
        algorithm = add(n, D + "alg", type=alg)
        for key, value in (params or {}).items():
            add(algorithm, D + "param", type=key, val=value)
        s = add(n, D + "shape")
        if shape:
            s.set("type", shape)
        if hidden:
            s.set("hideGeom", "1")
        adjustments = add(s, D + "adjLst")
        if shape == "roundRect":
            add(adjustments, D + "adj", idx=1, val=0.1)
        pres = add(n, D + "presOf")
        if axis:
            pres.set("axis", axis)
            if alg != "conn" and name != "linkText":
                pres.set("ptType", "node")
        return n, add(n, D + "constrLst"), add(n, D + "ruleLst")

    def c(parent, kind, **kw):
        return add(parent, D + "constr", type=kind, **kw)

    layout = E.Element(D + "layoutDef", nsmap={"dgm": D[1:-1]})
    root, rc, rr = node(layout, "run", "lin")
    loop = add(root, D + "forEach", axis="ch", ptType="node")
    group, gc, gr = node(loop, "card", "composite")
    title, tc, tr = node(
        group,
        "heading",
        "tx",
        "rect",
        hidden=True,
        axis="self",
        params={"parTxLTRAlign": "l", "parTxRTLAlign": "r", "txAnchorVert": "t"},
    )
    background, bc, br = node(group, "panel", "sp", "roundRect", axis="self")
    body, dc, dr = node(
        group, "detail", "tx", "roundRect", axis="des", style="paper", params={"stBulletLvl": "1"}
    )
    links = add(loop, D + "forEach", axis="followSib", ptType="sibTrans", cnt=1)
    link, ac, ar = node(
        links,
        "link",
        "conn",
        "conn",
        axis="self",
        params={"begPts": "auto", "endPts": "auto", "srcNode": "heading", "dstNode": "heading"},
    )
    linktext, xc, xr = node(
        link, "linkText", "tx", "conn", hidden=True, axis="self", params={"autoTxRot": "grav"}
    )
    c(rc, "w", **{"for": "ch", "forName": "card", "refType": "w"})
    c(
        rc,
        "w",
        **{
            "for": "ch",
            "ptType": "sibTrans",
            "refType": "w",
            "refFor": "ch",
            "refForName": "card",
            "fact": 0.25,
        },
    )
    for name in ("heading", "panel", "detail"):
        for dimension in ("w", "h"):
            c(rc, dimension, **{"for": "des", "forName": name})
    c(rc, "primFontSz", **{"for": "des", "forName": "heading", "val": 50})
    c(
        rc,
        "secFontSz",
        **{
            "for": "des",
            "forName": "detail",
            "refType": "primFontSz",
            "refFor": "des",
            "refForName": "heading",
        },
    )
    for op in ("equ", "lte"):
        c(
            rc,
            "primFontSz",
            **{
                "for": "des",
                "forName": "linkText",
                "refType": "primFontSz",
                "refFor": "des",
                "refForName": "heading",
                "fact": 0.8,
                "op": op,
            },
        )
    for name, factor in [("heading", 0.8), ("panel", 1.2), ("detail", 1.6)]:
        c(
            rc,
            "h",
            **{
                "for": "des",
                "forName": name,
                "refType": "primFontSz",
                "refFor": "des",
                "refForName": "heading",
                "fact": factor,
            },
        )
    for op in ("lte", "gte"):
        c(
            rc,
            "h",
            **{
                "for": "des",
                "forName": "panel",
                "refType": "h",
                "refFor": "des",
                "refForName": "heading",
                "op": op,
                "fact": 1.5,
            },
        )
    add(rr, D + "rule", type="w", **{"for": "ch", "forName": "card", "val": 0})
    add(rr, D + "rule", type="primFontSz", **{"for": "des", "forName": "heading", "val": 6})
    c(gc, "h", refType="w", fact=1000)
    for name in ("heading", "panel"):
        c(gc, "l", **{"for": "ch", "forName": name})
        c(gc, "t", **{"for": "ch", "forName": name})
    c(gc, "w", **{"for": "ch", "forName": "heading", "refType": "w", "fact": 0.75})
    c(gc, "l", **{"for": "ch", "forName": "detail", "refType": "w", "fact": 0.25})
    for name in ("panel", "detail"):
        c(
            gc,
            "w",
            **{
                "for": "ch",
                "forName": name,
                "refType": "w",
                "refFor": "ch",
                "refForName": "heading",
            },
        )
    c(
        gc,
        "t",
        **{
            "for": "ch",
            "forName": "detail",
            "refType": "h",
            "refFor": "ch",
            "refForName": "heading",
        },
    )
    for rules in (gr, tr, dr):
        add(rules, D + "rule", type="h", val="INF")
    c(tc, "h", refType="w", op="lte", fact=0.5)
    c(tc, "bMarg", refType="primFontSz", fact=0.3)
    for constraints in (tc, bc, dc):
        c(constraints, "h")
    c(dc, "secFontSz", val=50)
    c(dc, "primFontSz", refType="secFontSz")
    c(ac, "h", refType="w", fact=0.5)
    c(ac, "connDist")
    c(ac, "begPad", refType="connDist", fact=0.2)
    c(ac, "endPad", refType="connDist", fact=0.2)
    for side in "lrtb":
        c(xc, side + "Marg")
    add(xr, D + "rule", type="primFontSz", val=6)

    data = E.Element(D + "dataModel")
    points, connections = add(data, D + "ptLst"), add(data, D + "cxnLst")
    add(points, D + "pt", modelId="root", type="doc")
    for i, text in enumerate(("Plan", "Scope", "Ship", "Review")):
        p = add(points, D + "pt", modelId=str(i))
        t = add(p, D + "t")
        add(t, A + "bodyPr")
        add(t, A + "lstStyle")
        add(add(add(t, A + "p"), A + "r"), A + "t").text = text
        add(
            connections,
            D + "cxn",
            modelId="edge" + str(i),
            srcId="root" if i % 2 == 0 else str(i - 1),
            destId=str(i),
            srcOrd=i // 2 if i % 2 == 0 else 0,
        )
    for name, label in [
        ("heading", "ink"),
        ("panel", "ink"),
        ("detail", "paper"),
        ("link", "arrow"),
    ]:
        p = add(points, D + "pt", modelId="pres-" + name, type="pres")
        add(p, D + "prSet", presName=name, presStyleLbl=label)
    add(data, D + "bg")
    add(data, D + "whole")

    colors, style = E.Element(D + "colorsDef"), E.Element(D + "styleDef")
    for label in ("ink", "paper", "arrow"):
        pal = add(colors, D + "styleLbl", name=label)
        for kind in ("fill", "lin", "txFill", "effect", "txLin", "txEffect"):
            listing = add(pal, D + kind + "ClrLst")
            if kind in {"fill", "lin", "txFill"}:
                clr = add(
                    listing,
                    A + "schemeClr",
                    val="lt1" if label == "paper" and kind == "fill" else "accent1",
                )
                if label == "paper" and kind == "fill":
                    add(clr, A + "alpha", val=75000)
        ref = add(add(style, D + "styleLbl", name=label), D + "style")
        for kind, index in [
            ("fill", 1),
            ("ln", 0 if label == "arrow" else 2),
            ("effect", 0),
            ("font", "minor"),
        ]:
            add(ref, A + kind + "Ref", idx=index)

    with ZipFile(io.BytesIO(visual_pptx_bytes(smartart=True))) as z:
        parts = {info.filename: z.read(info) for info in z.infolist()}
    slide = E.fromstring(parts["ppt/slides/slide1.xml"])
    graphic = slide.find(".//" + A + "graphicData")
    ids = add(graphic, D + "relIds")
    rels = E.fromstring(parts["ppt/slides/_rels/slide1.xml.rels"])
    types = E.fromstring(parts["[Content_Types].xml"])
    for key, payload, relation, suffix in [
        ("lo", layout, "diagramLayout", "Layout"),
        ("dm", data, "diagramData", "Data"),
        ("cs", colors, "diagramColors", "Colors"),
        ("qs", style, "diagramQuickStyle", "Style"),
    ]:
        path = "ppt/diagrams/" + key + ".xml"
        ids.set(R + key, "rId-" + key)
        add(
            rels,
            "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship",
            Id="rId-" + key,
            Type=R[1:-1] + "/" + relation,
            Target="../diagrams/" + key + ".xml",
        )
        add(
            types,
            "{http://schemas.openxmlformats.org/package/2006/content-types}Override",
            PartName="/" + path,
            ContentType="application/vnd.openxmlformats-officedocument.drawingml.diagram"
            + suffix
            + "+xml",
        )
        parts[path] = E.tostring(payload)
    parts["ppt/slides/slide1.xml"] = E.tostring(slide)
    parts["ppt/slides/_rels/slide1.xml.rels"] = E.tostring(rels)
    parts["[Content_Types].xml"] = E.tostring(types)
    output = io.BytesIO()
    with ZipFile(output, "w") as z:
        for name, content in parts.items():
            z.writestr(name, content)
    return output.getvalue()
