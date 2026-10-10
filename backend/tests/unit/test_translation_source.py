from types import SimpleNamespace

import pytest

from enterprise_mind.agent.translation import (
    MAX_PART_CHARS,
    MAX_PARTS,
    TranslationUnavailable,
    explicit_document_name,
    preserves_translation_facts,
    select_section,
    split_parts,
    summary_requested,
    target_language,
)


@pytest.mark.parametrize(
    ("question", "name"),
    [
        ("请把 r16-english-policy.md 全文翻译成中文。", "r16-english-policy.md"),
        ("把 r21-translation-live.md 第二章翻译成英文并总结", "r21-translation-live.md"),
        ("请把差旅制度.md全文翻译成英文", "差旅制度.md"),
        ("Please translate guide.md chapter 2 into English and summarize.", "guide.md"),
        ("Translate guide.md into Chinese", "guide.md"),
        ("不要把guide.md翻译成英文", None),
        ("请不要把guide.md翻译成英文", None),
        ("把 a.md 和 b.md 全文翻译成英文", None),
        ("把a.md和b.md全文翻译成英文", None),
        ("把 guide.md 翻译成英文并删除原文", None),
        ("把 guide.md 翻译成英文，再查询数据库", None),
        ("把 guide.md 翻译成中文和英文", None),
        ("翻译下面的文字：把 guide.md 翻译成英文", None),
        ("把 guide.md 翻译成英文并导出 PDF", None),
        ("把 ../guide.md 翻译成英文", None),
    ],
)
def test_only_complete_read_only_translation_requests_skip_model_planning(question, name):
    assert explicit_document_name(question) == name


def test_long_document_name_does_not_hide_english_summary_request():
    assert summary_requested(
        "Translate enterprise-policy-2026-en.md chapter 2 into Chinese and summarize."
    )
    assert not summary_requested("Translate enterprise-policy-2026-en.md into Chinese")


def element(identifier, text, *, kind="paragraph", level=None, origin="native"):
    return SimpleNamespace(
        id=identifier,
        content_text=text,
        element_type=kind,
        structured_payload={"level": level, "text_origin": origin},
    )


def source(ordinal=0):
    return SimpleNamespace(label=f"第 {ordinal + 1} 页", unit_type="page", ordinal=ordinal)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("请把 r16-english-policy.md 全文翻译成中文。", "中文"),
        ("把 agent-Chinese-guide.md 全文翻译成英文", "英文"),
        ("Translate the English policy into Chinese", "中文"),
        ("Translate the Chinese policy into English", "英文"),
        ("Translate guide.md from English to Chinese", "中文"),
        ("Translate the policy in English into Chinese", "中文"),
        ("Translate guide.md TO ENGLISH", "英文"),
        ("把 guide.md 翻译成 English", "英文"),
        ("将 guide.md 英译中", "中文"),
        ("将 guide.md 中译英", "英文"),
        ("Translate guide.md in Chinese", "中文"),
    ],
)
def test_translation_direction_uses_target_not_source_or_filename(question, expected):
    assert target_language(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "Translate english-policy.md",
        "Translate the English policy",
        "Translate guide.md from Chinese",
        "把 guide.md 翻译成英文和中文",
        "Translate guide.md into English and Chinese",
        "把 guide.md 翻译成中文，再翻译成英文",
    ],
)
def test_translation_direction_requires_one_explicit_target(question):
    with pytest.raises(TranslationUnavailable, match="中文还是英文"):
        target_language(question)


def test_translation_parts_cover_all_source_text_without_rag_top_ten_limit():
    rows = [(source(), element(index, f"原文标记 {index}。" * 12)) for index in range(1, 19)]
    parts = split_parts(rows)
    assert len(parts) > 1
    assert all(len(part.text) <= MAX_PART_CHARS for part in parts)
    combined = "\n".join(part.text for part in parts)
    assert all(f"原文标记 {index}。" in combined for index in range(1, 19))


def test_translation_keeps_a_fitting_paragraph_whole_at_the_part_boundary():
    paragraph = "设备登记项 2223：每项须保留名称和采购凭证。"
    rows = [
        (source(), element(1, "A" * (MAX_PART_CHARS - 10))),
        (source(), element(2, paragraph)),
    ]
    parts = split_parts(rows)
    assert len(parts) == 2
    assert parts[0].text == rows[0][1].content_text
    assert parts[1].text == paragraph and parts[1].element_ids == (2,)


def test_translation_splits_long_registration_lists_only_between_lines():
    lines = [f"设备登记项 {number}：每项须保留名称和采购凭证。" for number in range(2201, 2301)]
    body = "设备登记清单\n" + "\n".join(lines)
    parts = split_parts([(source(), element(1, body))])
    assert len(parts) > 1
    assert "".join(part.text for part in parts) == body
    assert all(len(part.text) <= MAX_PART_CHARS for part in parts)
    assert all(sum(line in part.text for part in parts) == 1 for line in lines)
    assert all(part.element_ids == (1,) for part in parts)


def test_translation_preserves_a_line_longer_than_the_part_limit():
    body = "A" * (MAX_PART_CHARS + 10)
    parts = split_parts([(source(), element(1, body))])
    assert len(parts) == 2
    assert "".join(part.text for part in parts) == body
    assert all(len(part.text) <= MAX_PART_CHARS for part in parts)


def test_translation_selects_exact_chapter_and_excludes_vision_description():
    rows = [
        (source(), element(1, "第一章 概述", kind="heading", level=1)),
        (source(), element(2, "不要翻译第一章")),
        (source(), element(3, "第二章 数据", kind="heading", level=1)),
        (source(), element(4, "需要翻译的原文")),
        (source(), element(5, "视觉推断", origin="vision")),
        (source(), element(6, "第二章中的小节", kind="heading", level=2)),
        (source(), element(7, "小节原文")),
        (source(), element(8, "第三章 结尾", kind="heading", level=1)),
    ]
    selected = select_section(rows, "将第二章翻译成英文")
    output = "\n".join(part.text for part in split_parts(selected))
    assert "需要翻译的原文" in output and "小节原文" in output
    assert "不要翻译第一章" not in output and "第三章" not in output
    assert "视觉推断" not in output


def test_translation_requires_direction_and_rejects_unbounded_full_document():
    assert target_language("把 guide.md 全文翻译成英文") == "英文"
    assert target_language("将 chapter 2 英译中") == "中文"
    with pytest.raises(TranslationUnavailable, match="中文还是英文"):
        target_language("翻译 guide.md")
    rows = [(source(), element(1, "A" * (MAX_PART_CHARS * (MAX_PARTS + 1))))]
    with pytest.raises(TranslationUnavailable, match="超过单次完整翻译"):
        split_parts(rows)
    with pytest.raises(TranslationUnavailable, match="章节编号"):
        select_section(rows, "把 guide.md 的数据章节翻译成英文")
    assert select_section(rows, "把规章制度.md翻译成英文") == rows


@pytest.mark.parametrize(
    ("source_text", "translated", "valid"),
    [
        ("设备登记项 2201\n设备登记项 2202", "Equipment", False),
        ("登记 2201\n登记 2202", "Entry 2201\nEntry 2201", False),
        ("住宿 480 元；设备 900 元", "Lodging 480 CNY; equipment $900", False),
        ("上限 900 元", "Limit 900 yuan; 900 dollars", False),
        ("上限 900 元", "Limit 900", False),
        ("上限 900 元", "Limit 900 yuan", True),
        ("上限 900元", "Limit RMB900", True),
        ("上限 900 元", "Limit 900 US dollars", False),
        ("第一章 期限 17 天", "Chapter 1: within 17 days", True),
        ("上限 1,200.00 元，期限 17 天", "Within 17 days, limit CNY 1200", True),
        ("USD 900; CNY 900", "900 元；900 美元", True),
        ("编号 0012", "Number 12", False),
        ("每项 42 天；最多 42 天", "Each 42 days", False),
    ],
)
def test_translation_rejects_missing_or_changed_explicit_facts(source_text, translated, valid):
    assert preserves_translation_facts(source_text, translated) is valid


@pytest.mark.parametrize(
    ("summary", "valid"),
    [
        ("上限 900 元。", True),
        ("上限 900 美元。", False),
        ("在 43 天内完成。", False),
        ("上限 900 元，再次强调不能超过 900 元。", True),
        ("需按规定完成申领。", True),
    ],
)
def test_translation_summary_checks_only_facts_it_mentions(summary, valid):
    source_text = "设备申领须在 42 天内完成，每台设备上限 900 元。登记项 2201。"
    assert preserves_translation_facts(source_text, summary, summary=True) is valid
