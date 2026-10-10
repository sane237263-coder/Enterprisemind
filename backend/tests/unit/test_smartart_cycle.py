import io
import math
from zipfile import ZipFile

import pytest
from lxml import etree as E
from smartart_cycle_samples import cycle_pptx_bytes
from smartart_samples import A, D, P

from enterprise_mind.ingestion import smartart_linear as common
from enterprise_mind.ingestion.smartart_render import smartart_render_copy


class Metrics:
    line_height = 1.25

    def lines(self, text, width, size):
        return max(1, math.ceil(len(text) * size * 0.5 / width))


@pytest.fixture(autouse=True)
def metrics(monkeypatch):
    monkeypatch.setattr(common, "FontMetrics", lambda family: Metrics())


def modify(source, transform):
    result = io.BytesIO()
    with ZipFile(io.BytesIO(source)) as original, ZipFile(result, "w") as output:
        for item in original.infolist():
            contents = original.read(item)
            if item.filename.endswith(".xml"):
                root = E.fromstring(contents)
                transform(item.filename, root)
                contents = E.tostring(root)
            output.writestr(item, contents)
    return result.getvalue()


def lowered(source):
    with ZipFile(io.BytesIO(source)) as archive:
        return common.linear_render_parts(archive)


@pytest.mark.parametrize("count", [3, 4, 5, 6, 8])
def test_flat_cycle_preserves_round_nodes_text_and_original_package(count, tmp_path):
    content = cycle_pptx_bytes(count)
    source, target = tmp_path / "source.pptx", tmp_path / "render.pptx"
    source.write_bytes(content)
    assert smartart_render_copy(source, target)
    assert source.read_bytes() == content
    with ZipFile(source) as original, ZipFile(target) as output:
        assert original.namelist() == output.namelist()
        assert [
            name for name in original.namelist() if original.read(name) != output.read(name)
        ] == ["ppt/slides/slide1.xml"]
        slide = E.fromstring(output.read("ppt/slides/slide1.xml"))
        assert not slide.findall(".//" + D + "relIds")
        shapes = slide.findall(".//" + P + "grpSp/" + P + "sp")
        assert len(shapes) == count * 2
        for node in shapes[::2]:
            extent = node.find("./" + P + "spPr/" + A + "xfrm/" + A + "ext")
            assert extent.get("cx") == extent.get("cy")
        assert [t.text for t in slide.iter(A + "t")] == [chr(65 + i) for i in range(count)]
        ids = [item.get("id") for item in slide.iter(P + "cNvPr")]
        assert len(ids) == len(set(ids))
    assert not smartart_render_copy(target, tmp_path / "again.pptx")


def test_cycle_uses_source_names_frame_and_explicit_connector_padding():
    source = cycle_pptx_bytes()

    def change(name, root):
        if name == "ppt/slides/slide1.xml":
            tr = root.find(".//" + P + "graphicFrame/" + P + "xfrm")
            tr.find(A + "ext").set("cx", "4000000")
            tr.find(A + "off").set("x", "1000000")
        if name in {"ppt/diagrams/lo.xml", "ppt/diagrams/dm.xml"}:
            for node in root.iter():
                for key in ("name", "presName", "forName"):
                    if node.get(key) in {"ring", "item", "arrow", "label"}:
                        node.set(key, "renamed-" + node.get(key))
        if name == "ppt/diagrams/lo.xml":
            connector = next(
                node for node in root.iter(D + "layoutNode") if node.get("name") == "renamed-arrow"
            )
            constraints = connector.find(D + "constrLst")
            for kind in ("begPad", "endPad"):
                E.SubElement(constraints, D + "constr", type=kind, refType="connDist", fact="0.1")

    normal = E.fromstring(lowered(source)["ppt/slides/slide1.xml"])
    altered = E.fromstring(lowered(modify(source, change))["ppt/slides/slide1.xml"])
    group = altered.find(".//" + P + "grpSp")
    assert group.find("./" + P + "grpSpPr/" + A + "xfrm/" + A + "off").get("x") == "1000000"
    shapes = altered.findall(".//" + P + "grpSp/" + P + "sp")
    assert shapes[1].find("./" + P + "nvSpPr/" + P + "cNvPr").get("name") == "renamed-arrow"
    old_width = (
        normal.findall(".//" + P + "grpSp/" + P + "sp")[1]
        .find("./" + P + "spPr/" + A + "xfrm/" + A + "ext")
        .get("cx")
    )
    new_width = shapes[1].find("./" + P + "spPr/" + A + "xfrm/" + A + "ext").get("cx")
    assert int(new_width) > int(old_width)


@pytest.mark.parametrize(
    "case",
    [
        "unknown_constraint",
        "unknown_algorithm",
        "nested_data",
        "reverse",
        "custom_text",
        "text_does_not_fit",
        "nan",
        "extra_node",
        "padding_overlap",
    ],
)
def test_unsupported_cycle_is_left_intact_without_partial_replacement(case):
    def change(name, root):
        if name == "ppt/diagrams/lo.xml":
            node = next(n for n in root.iter(D + "layoutNode") if n.get("name") == "item")
            if case == "unknown_constraint":
                E.SubElement(node.find(D + "constrLst"), D + "constr", type="userZ", val="10")
            elif case == "unknown_algorithm":
                E.SubElement(node.find(D + "alg"), D + "param", type="autoTxRot", val="grav")
            elif case == "nan":
                node.find(D + "constrLst/" + D + "constr").set("fact", "NaN")
            elif case == "extra_node":
                E.SubElement(node, D + "layoutNode", name="unhandled")
            elif case == "padding_overlap":
                arrow = next(n for n in root.iter(D + "layoutNode") if n.get("name") == "arrow")
                E.SubElement(
                    arrow.find(D + "constrLst"),
                    D + "constr",
                    type="begPad",
                    refType="connDist",
                    fact="1",
                )
        if name == "ppt/diagrams/dm.xml":
            if case == "nested_data":
                root.find(D + "cxnLst")[1].set("srcId", "0")
            if case == "reverse":
                E.SubElement(
                    E.SubElement(
                        E.SubElement(root.find(D + "ptLst")[0], D + "prSet"), D + "presLayoutVars"
                    ),
                    D + "dir",
                    val="rev",
                )
            if case == "custom_text":
                E.SubElement(next(root.iter(A + "r")), A + "rPr", sz="12000")
            if case == "text_does_not_fit":
                next(root.iter(A + "t")).text = "Unbounded " * 1000

    assert lowered(modify(cycle_pptx_bytes(), change)) == {}


def test_small_cycle_and_missing_font_keep_original(monkeypatch):
    assert lowered(cycle_pptx_bytes(2)) == {}

    def missing(family):
        raise common.UnsupportedLayout

    monkeypatch.setattr(common, "FontMetrics", missing)
    assert lowered(cycle_pptx_bytes()) == {}
