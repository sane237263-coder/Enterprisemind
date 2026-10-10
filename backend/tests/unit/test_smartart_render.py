import io
from zipfile import ZipFile

import pytest
from lxml import etree
from media_samples import visual_pptx_bytes

from enterprise_mind.infrastructure.settings import WorkerConfig
from enterprise_mind.ingestion.rich_parsing import check_package
from enterprise_mind.ingestion.smartart_render import (
    DGM,
    _explicit_transition_names,
    smartart_render_copy,
)


def layout_xml(*, transition="bridge", parent="pair", kind="sibTrans", dimension="w"):
    return f'''<dgm:layoutDef xmlns:dgm="{DGM[1:-1]}">
      <dgm:layoutNode name="root"><dgm:alg type="lin"/>
        <dgm:constrLst><dgm:constr type="{dimension}" for="ch" ptType="{kind}"
          refType="{dimension}" refFor="ch" refForName="{parent}" fact="0.3333"/></dgm:constrLst>
        <dgm:forEach axis="ch" ptType="node">
          <dgm:layoutNode name="{parent}"><dgm:presOf axis="self"/></dgm:layoutNode>
          <dgm:forEach axis="followSib" ptType="{kind}">
            <dgm:layoutNode name="{transition}"><dgm:presOf axis="self"/></dgm:layoutNode>
          </dgm:forEach>
        </dgm:forEach>
      </dgm:layoutNode></dgm:layoutDef>'''.encode()


@pytest.mark.parametrize("kind,dimension", [("sibTrans", "w"), ("parTrans", "h")])
def test_transition_constraint_uses_declared_name_not_preset_or_fixture(kind, dimension):
    content = layout_xml(
        transition="renamed-link", parent="renamed-parent", kind=kind, dimension=dimension
    )
    original = etree.fromstring(content).find(".//" + DGM + "constr")
    updated = _explicit_transition_names(content)
    constraint = etree.fromstring(updated).find(".//" + DGM + "constr")
    assert dict(constraint.attrib) == {**original.attrib, "forName": "renamed-link"}
    assert _explicit_transition_names(updated) == updated


@pytest.mark.parametrize(
    "before,after",
    [
        (b'for="ch"', b'for="des"'),
        (b'ptType="sibTrans"', b'ptType="all"'),
        (b'for="ch"', b'for="ch" forName="already-explicit"'),
        (b'refForName="pair"', b'refForName="missing"'),
        (b'refForName="pair"', b'refForName="bridge"'),
        (b'fact="0.3333"', b'fact="NaN"'),
        (b'fact="0.3333"', b'fact="0"'),
        (b'fact="0.3333"', b'fact="2"'),
        (b'fact="0.3333"', b'fact="invalid"'),
        (b'fact="0.3333"', b'fact="0.3333" op="lte"'),
        (b'axis="followSib"', b'axis="ch followSib"'),
        (b'axis="followSib"', b'axis="followSib" ref="another-loop"'),
        (b'<dgm:alg type="lin"/>', b'<dgm:alg type="cycle"/>'),
        (b'<dgm:layoutNode name="bridge">', b'<dgm:layoutNode name="pair">'),
    ],
)
def test_ambiguous_or_other_constraints_are_left_unchanged(before, after):
    content = layout_xml().replace(before, after)
    assert _explicit_transition_names(content) == content


def test_conditional_and_multiple_targets_are_not_guessed():
    content = layout_xml()
    original = b'<dgm:layoutNode name="bridge"><dgm:presOf axis="self"/></dgm:layoutNode>'
    for replacement in (
        original + original.replace(b'"bridge"', b'"other"'),
        b"<dgm:choose><dgm:if>" + original + b"</dgm:if></dgm:choose>",
        b'<dgm:choose><dgm:else><dgm:forEach axis="ch" ptType="sibTrans">'
        + original
        + b"</dgm:forEach></dgm:else></dgm:choose>",
    ):
        candidate = content.replace(original, replacement)
        assert _explicit_transition_names(candidate) == candidate


def test_linear_direction_choice_and_default_presentation_keep_transition_binding():
    content = (
        layout_xml()
        .replace(
            b'<dgm:alg type="lin"/>',
            b'<dgm:choose><dgm:if><dgm:alg type="lin"/></dgm:if>'
            b'<dgm:else><dgm:alg type="lin"><dgm:param type="linDir" val="fromR"/>'
            b"</dgm:alg></dgm:else></dgm:choose>",
        )
        .replace(b'<dgm:presOf axis="self"/>', b"<dgm:presOf/>")
    )
    updated = etree.fromstring(_explicit_transition_names(content))
    assert updated.find(".//" + DGM + "constr").get("forName") == "bridge"


def test_unknown_sibling_cannot_be_excluded_from_point_type_selection():
    content = layout_xml().replace(
        b'<dgm:alg type="lin"/>',
        b'<dgm:alg type="lin"/><dgm:layoutNode name="unknown">'
        b'<dgm:presOf axis="des"/></dgm:layoutNode>',
    )
    assert _explicit_transition_names(content) == content


def test_layout_xml_does_not_expand_entities():
    content = b'<!DOCTYPE layout [<!ENTITY name SYSTEM "file:///missing">]>' + layout_xml()
    with pytest.raises(ValueError, match="DTD"):
        _explicit_transition_names(content)


def test_render_copy_preserves_original_package_and_all_other_parts(tmp_path):
    source = tmp_path / "source.pptx"
    target = tmp_path / "render.pptx"
    with ZipFile(io.BytesIO(visual_pptx_bytes())) as original, ZipFile(source, "w") as archive:
        for info in original.infolist():
            archive.writestr(info, original.read(info))
        archive.writestr("ppt/diagrams/layout1.xml", layout_xml())
        archive.writestr("ppt/diagrams/data1.xml", '<dgm:dataModel xmlns:dgm="' + DGM[1:-1] + '"/>')
    before = source.read_bytes()
    check_package(source, "pptx", WorkerConfig(), forbid_external_relationships=True)
    assert smartart_render_copy(source, target)
    assert source.read_bytes() == before
    check_package(target, "pptx", WorkerConfig(), forbid_external_relationships=True)
    with ZipFile(source) as original, ZipFile(target) as rendered:
        assert original.namelist() == rendered.namelist()
        changed = [
            name for name in original.namelist() if original.read(name) != rendered.read(name)
        ]
        assert changed == ["ppt/diagrams/layout1.xml"]
    assert not smartart_render_copy(target, tmp_path / "unneeded.pptx")
    assert not (tmp_path / "unneeded.pptx").exists()


def test_ordinary_presentation_does_not_create_a_render_copy(tmp_path):
    source = tmp_path / "source.pptx"
    source.write_bytes(visual_pptx_bytes())
    target = tmp_path / "render.pptx"
    assert not smartart_render_copy(source, target)
    assert not target.exists()
