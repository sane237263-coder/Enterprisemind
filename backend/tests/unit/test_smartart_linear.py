import io
import math
from copy import deepcopy
from zipfile import ZipFile

import pytest
from lxml import etree as E
from smartart_samples import A, D, P, linear_pptx_bytes

from enterprise_mind.ingestion import smartart_linear as linear
from enterprise_mind.ingestion.smartart_render import smartart_render_copy


class Metrics:
    """Deterministic unit-test metrics; real font/render checks run in Linux."""

    line_height = 1.25

    def lines(self, text, width, size):
        return max(1, math.ceil(len(text) * size * 0.5 / width))


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(linear, "FontMetrics", lambda family: Metrics())
    return linear_pptx_bytes()


def rewrite(source, transform):
    stream = io.BytesIO()
    with ZipFile(io.BytesIO(source)) as original, ZipFile(stream, "w") as out:
        for info in original.infolist():
            content = original.read(info)
            if info.filename.endswith(".xml"):
                root = E.fromstring(content)
                transform(info.filename, root)
                content = E.tostring(root)
            out.writestr(info, content)
    return stream.getvalue()


def replacements(source):
    with ZipFile(io.BytesIO(source)) as archive:
        return linear.linear_render_parts(archive)


def test_complete_render_copy_preserves_source_text_styles_and_other_parts(source, tmp_path):
    original = tmp_path / "original.pptx"
    original.write_bytes(source)
    output = tmp_path / "render.pptx"
    assert smartart_render_copy(original, output)
    assert original.read_bytes() == source
    with ZipFile(original) as before, ZipFile(output) as after:
        changed = {name for name in before.namelist() if before.read(name) != after.read(name)}
        assert changed == {"ppt/slides/slide1.xml", "ppt/diagrams/lo.xml"}
        slide = E.fromstring(after.read("ppt/slides/slide1.xml"))
        assert not slide.findall(".//" + D + "relIds")
        assert [n.text for n in slide.iter(A + "t")] == ["Scope", "Plan", "Review", "Ship"]
        assert len(slide.findall(".//" + P + "sp")) == 7
        assert len(slide.findall(".//" + A + "alpha")) == 2
        assert {n.get("val") for n in slide.iter(A + "alpha")} == {"75000"}
        ids = [n.get("id") for n in slide.iter(P + "cNvPr")]
        assert len(ids) == len(set(ids))
    assert not smartart_render_copy(output, tmp_path / "second.pptx")


def test_names_and_frame_dimensions_are_inputs_not_preset_constants(source):
    def modify(name, root):
        if name.startswith("ppt/diagrams/"):
            mapping = {
                n: "renamed-" + n
                for n in ("run", "card", "heading", "panel", "detail", "link", "linkText")
            }
            for node in root.iter():
                for key in ("name", "forName", "refForName", "presName"):
                    if node.get(key) in mapping:
                        node.set(key, mapping[node.get(key)])
                if node.get("type") in {"srcNode", "dstNode"}:
                    node.set("val", mapping[node.get("val")])
        if name == "ppt/slides/slide1.xml":
            transform = root.find(".//" + P + "graphicFrame/" + P + "xfrm")
            transform.find(A + "off").set("x", "1800000")
            transform.find(A + "ext").set("cx", "4000000")

    candidate = rewrite(source, modify)
    changed = replacements(candidate)
    slide = E.fromstring(changed["ppt/slides/slide1.xml"])
    group = slide.find(".//" + P + "grpSp")
    assert group.find("./" + P + "grpSpPr/" + A + "xfrm/" + A + "off").get("x") == "1800000"
    assert all(
        n.get("name").startswith("renamed-")
        for n in group.findall("./" + P + "sp/" + P + "nvSpPr/" + P + "cNvPr")
    )
    assert {n.get("sz") for n in slide.iter(A + "rPr")} != {"3600"}


@pytest.mark.parametrize(
    "case",
    [
        "gte",
        "unknown_constraint",
        "unknown_algorithm",
        "nan",
        "font",
        "custom_geometry",
        "custom_text",
        "reverse",
        "unrecognized_loop",
        "extra_loop_child",
        "visible_group",
    ],
)
def test_unknown_or_conflicting_semantics_do_not_partially_replace_frame(source, case):
    def modify(name, root):
        if name == "ppt/diagrams/lo.xml":
            if case == "gte":
                next(c for c in root.iter(D + "constr") if c.get("op") == "gte").set("fact", "2")
            elif case == "unknown_constraint":
                E.SubElement(
                    root.find(".//" + D + "constrLst"), D + "constr", type="diam", val="10"
                )
            elif case == "unknown_algorithm":
                root.find(".//" + D + "alg").set("type", "cycle")
            elif case == "nan":
                next(c for c in root.iter(D + "constr") if c.get("ptType") == "sibTrans").set(
                    "fact", "NaN"
                )
            elif case == "reverse":
                variables = E.SubElement(root.find(D + "layoutNode"), D + "varLst")
                E.SubElement(variables, D + "dir", val="rev")
            elif case == "unrecognized_loop":
                root.find(".//" + D + "forEach").set("cnt", "1")
            elif case == "extra_loop_child":
                E.SubElement(root.find(".//" + D + "forEach"), D + "presOf", axis="des")
            elif case == "visible_group":
                root.find("./" + D + "layoutNode/" + D + "shape").set("type", "rect")
        if name == "ppt/diagrams/dm.xml":
            if case == "custom_geometry":
                props = E.SubElement(root.find(".//" + D + "pt"), D + "spPr")
                E.SubElement(props, A + "xfrm")
            elif case == "custom_text":
                run = root.find(".//" + A + "r")
                run.insert(0, E.Element(A + "rPr", b="1"))
        if name == "ppt/theme/theme1.xml" and case == "font":
            root.find(".//" + A + "minorFont/" + A + "latin").set("typeface", "UnknownFont")

    if case == "font":
        # Test the real font-family support check, independently of local fonts.
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                linear, "FontMetrics", lambda _: (_ for _ in ()).throw(linear.UnsupportedLayout())
            )
            assert replacements(rewrite(source, modify)) == {}
    else:
        assert replacements(rewrite(source, modify)) == {}


def test_no_text_fit_keeps_original_frame(source):
    def modify(name, root):
        if name == "ppt/slides/slide1.xml":
            root.find(".//" + P + "graphicFrame/" + P + "xfrm/" + A + "ext").set("cy", "100")

    assert replacements(rewrite(source, modify)) == {}


def test_xml_entities_are_rejected():
    with pytest.raises(ValueError, match="DTD"):
        linear.parse(b'<!DOCTYPE data [<!ENTITY x SYSTEM "file:///secret">]><data>&x;</data>')


def test_multiple_frames_keep_unique_ids_and_decline_only_unsupported_frame(source):
    def modify(name, root):
        if name == "ppt/slides/slide1.xml":
            frame = root.find(".//" + P + "graphicFrame")
            for shape_id in (100, 200):
                copied = deepcopy(frame)
                copied.find(".//" + P + "cNvPr").set("id", str(shape_id))
                if shape_id == 200:
                    copied.find("./" + P + "xfrm/" + A + "ext").set("cy", "100")
                frame.getparent().append(copied)

    slide = E.fromstring(replacements(rewrite(source, modify))["ppt/slides/slide1.xml"])
    assert len(slide.findall(".//" + P + "grpSp")) == 2
    assert len(slide.findall(".//" + D + "relIds")) == 1
    ids = [n.get("id") for n in slide.iter(P + "cNvPr")]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("from_data", [False, True])
def test_native_drawing_cache_is_preserved(source, from_data):
    stream = io.BytesIO()
    rel_namespace = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    rel_path = "ppt/diagrams/_rels/dm.xml.rels" if from_data else "ppt/slides/_rels/slide1.xml.rels"
    with ZipFile(io.BytesIO(source)) as original, ZipFile(stream, "w") as out:
        for info in original.infolist():
            if info.filename != rel_path:
                out.writestr(info, original.read(info))
        rels = (
            E.Element(rel_namespace + "Relationships")
            if from_data
            else E.fromstring(original.read(rel_path))
        )
        E.SubElement(
            rels,
            rel_namespace + "Relationship",
            Id="native-cache",
            Type="http://schemas.microsoft.com/office/2007/relationships/diagramDrawing",
            Target="drawing.xml" if from_data else "../diagrams/drawing.xml",
        )
        out.writestr(rel_path, E.tostring(rels))
        out.writestr(
            "ppt/diagrams/drawing.xml",
            b'<drawing xmlns="http://schemas.microsoft.com/office/drawing/2008/diagram"><sp/></drawing>',
        )
    assert replacements(stream.getvalue()) == {}
