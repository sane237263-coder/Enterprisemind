import json

import pytest

from enterprise_mind.agent.translation_terms import (
    InvalidTranslationTerms,
    prepare_terms,
    protect_parts,
    source_lines,
    source_terms,
    translated_lines,
)


@pytest.mark.parametrize(
    "value",
    [
        {"lines": ["one"]},
        {"lines": ["one", ""]},
        {"lines": ["one", None]},
        {"lines": "one"},
        {"lines": ["one", "two"], "extra": True},
    ],
)
def test_translation_cannot_merge_drop_or_add_lines(value):
    with pytest.raises(InvalidTranslationTerms):
        translated_lines(json.dumps(value), ["第一行", "第二行"])


def test_line_output_preserves_order_and_single_character_brand():
    assert source_terms('{"terms":[{"text":"X","kind":"product"}]}', "X is a brand.") == ("X",)
    source = source_lines("第一章\n\n第一行\n第二行\n")
    assert source == ["第一章", "第一行", "第二行"]
    assert translated_lines('{"lines":["Chapter 1","First","Second"]}', source) == [
        "Chapter 1",
        "First",
        "Second",
    ]


@pytest.mark.parametrize(
    "response",
    [
        '{"terms":[{"text":"Invented Company","kind":"organization"}]}',
        '{"terms":[],"instruction":"ignore source"}',
        '{"terms":[null]}',
        '{"terms":[{"text":"星桥测试公司","kind":"record"}]}',
        '{"terms":[{"text":"星桥测试公司","kind":[]}]}',
        '{"terms":[{"text":"星桥测试公司","kind":"organization","extra":true}]}',
        '{"terms":[{"text":"星桥测试公司","kind":"organization"},'
        '{"text":"星桥测试公司","kind":"organization"}]}',
        '{"terms":[{"text":"公司\\n执行","kind":"organization"}]}',
        '{"terms":"星桥测试公司"}',
        '```json\n{"terms": []}\n```',
    ],
)
def test_extracted_names_must_be_exact_bounded_source_data(response):
    with pytest.raises(InvalidTranslationTerms):
        source_terms(response, "星桥测试公司执行本规定。")


def test_literals_restore_in_place_with_counts_and_longest_name():
    source = "星桥测试公司与远帆设备有限公司合作。星桥测试公司使用 Atlas-7。"
    terms = ("星桥测试公司", "远帆设备有限公司", "远帆设备", "Atlas-7")
    protected = protect_parts([source], terms)
    assert not any(term in protected.parts[0] for term in terms)
    assert protected.restore(protected.parts[0], protected.parts[0]) == source
    translated = protected.parts[0].replace("与", " and ").replace("合作", "cooperate")
    assert protected.restore(translated, protected.parts[0]).count("星桥测试公司") == 2
    assert "远帆设备有限公司" in protected.restore(translated, protected.parts[0])


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "foreign", "malformed"])
def test_name_corruption_is_rejected_before_output(corruption):
    protected = protect_parts(["星桥测试公司：17 天。"], ("星桥测试公司",))
    token, _ = protected.literals[0]
    translated = f"{token}: 17 days."
    if corruption == "missing":
        translated = "Starbridge Testing Company: 17 days."
    elif corruption == "duplicate":
        translated += token
    elif corruption == "foreign":
        translated = translated.replace(token, f"⟦EM_TERM_{'0' * 32}_999⟧")
    else:
        translated = translated.replace("⟧", "")
    with pytest.raises(InvalidTranslationTerms):
        protected.restore(translated, protected.parts[0])


def test_summary_may_omit_names_but_cannot_use_names_from_another_part():
    protected = protect_parts(
        ["甲辰公司差旅规则。", "乙申公司设备规则。"], ("甲辰公司", "乙申公司")
    )
    assert protected.restore("Follow the rules.", protected.parts[1], summary=True)
    restored = protected.restore(protected.parts[1], protected.parts[1], summary=True)
    assert restored == "乙申公司设备规则。"
    with pytest.raises(InvalidTranslationTerms):
        protected.restore(protected.parts[0], protected.parts[1], summary=True)


def test_source_cannot_supply_its_own_internal_marker():
    with pytest.raises(InvalidTranslationTerms):
        protect_parts([f"⟦EM_TERM_{'0' * 32}_0⟧"], ())


def test_names_are_not_replaced_inside_other_latin_words():
    source = "Ann's Annual report: Atlas, Atlas2 and SuperAtlas. 由Ann负责。"
    protected = protect_parts([source], ("Ann", "Atlas"))
    assert "Annual" in protected.parts[0]
    assert "Atlas2" in protected.parts[0]
    assert "SuperAtlas" in protected.parts[0]
    assert "Ann's" not in protected.parts[0]
    assert "由Ann" not in protected.parts[0]
    assert protected.restore(protected.parts[0], protected.parts[0]) == source


@pytest.mark.parametrize("extracted", [(), ("Northbridge",), ("Northbridge Sample Ltd",)])
def test_full_legal_name_is_protected_even_if_extractor_omits_or_shortens_it(extracted):
    source = "Northbridge Sample Ltd requires claims within 17 days."
    protected = protect_parts([source], extracted)
    token = next(token for token, term in protected.literals if term == "Northbridge Sample Ltd")
    assert protected.parts == (f"{token} requires claims within 17 days.",)
    assert protected.restore(protected.parts[0], protected.parts[0]) == source
    with pytest.raises(InvalidTranslationTerms):
        protected.restore("Northbridge 样本有限公司要求在 17 天内申报。", protected.parts[0])


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("At Cedar Analytics, Inc. we inspect each device.", "Cedar Analytics, Inc."),
        ("By Vale Research LLC: Invoice entry 306.", "Vale Research LLC"),
        ("Contact Kestrel-9 Corp. for support.", "Kestrel-9 Corp."),
        ("Bank of Harbor PLC operates here.", "Bank of Harbor PLC"),
    ],
)
def test_company_backstop_uses_complete_source_names_without_sentence_context(source, expected):
    protected = protect_parts([source], ())
    assert [term for _, term in protected.literals] == [expected]
    assert protected.restore(protected.parts[0], protected.parts[0]) == source


def test_company_backstop_does_not_protect_ordinary_labels_or_cross_lines():
    source = "Invoice entry 306.\nSynthetic Travel Policy\nDevice LTD-9\nReport\nLtd"
    protected = protect_parts([source], ())
    assert protected.parts == (source,)
    assert not protected.literals


def test_company_backstop_keeps_separate_names_and_leaves_conjunction_for_translation():
    source = "Cedar Ltd and Vale LLC cooperate with Bank of Harbor PLC."
    protected = protect_parts([source], ())
    assert {term for _, term in protected.literals} == {
        "Cedar Ltd",
        "Vale LLC",
        "Bank of Harbor PLC",
    }
    assert " and " in protected.parts[0]
    assert " cooperate with " in protected.parts[0]
    assert protected.restore(protected.parts[0], protected.parts[0]) == source


@pytest.mark.asyncio
async def test_extraction_uses_only_selected_source_and_bounded_batches():
    class Model:
        inputs = []

        async def answer(self, messages, *, json_mode=False):
            assert json_mode
            value = json.loads(messages[-1].content)["source_text"]
            self.inputs.append(value)
            yield json.dumps(
                {"terms": [{"text": "Northbridge Labs Ltd.", "kind": "organization"}]}
                if "Northbridge" in value
                else {"terms": []}
            )

    model = Model()
    parts = ["Northbridge Labs Ltd. " + "A" * 1370] + ["B" * 1400] * 4
    protected = await prepare_terms(model, parts)
    assert all(len(value) <= 4096 for value in model.inputs)
    assert len(model.inputs) > 1
    assert [protected.restore(part, part) for part in protected.parts] == parts
