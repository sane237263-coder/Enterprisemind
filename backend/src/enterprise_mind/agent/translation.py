"""Bounded, source-ordered translation of one authorized active document."""

import re
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select

from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.models import (
    ContentElement,
    Document,
    Revision,
    SourceAnchor,
    SourceUnit,
)
from enterprise_mind.knowledge.models import KnowledgeBase
from enterprise_mind.rag.citations import CitationCandidate
from enterprise_mind.rag.models import Chunk, ChunkAnchor
from enterprise_mind.rag.repository import Anchor, RetrievalRepository, Scope

MAX_PART_CHARS = 1400
MAX_PARTS = 16
SECTION = re.compile(
    r"第[一二三四五六七八九十百零〇0-9]+[章节]|\b(?:chapter|section)\s+\d+\b", re.I
)
CHINESE_NUMBERS = {char: value for value, char in enumerate("零一二三四五六七八九")}
CHINESE_NUMBERS["〇"] = 0

NUMBER = r"\d+(?:,\d{3})*(?:\.\d+)?"
CURRENCY = (
    r"(?<![A-Za-z])(?:CNY|RMB|USD|yuan|renminbi|(?:US\s+)?dollars?)(?![A-Za-z])"
    r"|人民币|美元|元|US\s*\$|[$¥￥]"
)
MONEY = re.compile(
    rf"(?P<prefix>{CURRENCY})\s*(?P<value>{NUMBER})"
    rf"|(?P<amount>{NUMBER})\s*(?P<suffix>{CURRENCY})",
    re.I,
)


def preserves_translation_facts(
    source_text: str, translated: str, *, summary: bool = False
) -> bool:
    """Check explicit numeric occurrences and CNY/USD amounts, not semantic completeness."""

    def number(value: str) -> str:
        value = value.replace(",", "")
        return value.rstrip("0").rstrip(".") if "." in value else value

    def numbers(value: str) -> Counter[str]:
        return Counter(number(match) for match in re.findall(NUMBER, value))

    def money(value: str) -> Counter[tuple[str, str]]:
        result: Counter[tuple[str, str]] = Counter()
        for match in MONEY.finditer(value):
            currency = (match["prefix"] or match["suffix"]).lower()
            kind = (
                "USD"
                if "$" in currency
                or currency in {"usd", "美元"}
                or currency.endswith(("dollar", "dollars"))
                else "CNY"
            )
            result[(number(match["value"] or match["amount"]), kind)] += 1
        return result

    if summary:
        return set(numbers(translated)) <= set(numbers(source_text)) and set(
            money(translated)
        ) <= set(money(source_text))
    return not (numbers(source_text) - numbers(translated)) and money(source_text) == money(
        translated
    )


class TranslationUnavailable(Exception):
    """A complete translation cannot be promised for this request."""


@dataclass(frozen=True)
class TranslationPart:
    text: str
    element_ids: tuple[int, ...]
    source_label: str
    citation: CitationCandidate | None = None


@dataclass(frozen=True)
class TranslationWork:
    title: str
    target_language: str
    parts: tuple[TranslationPart, ...]


def explicit_document_name(question: str) -> str | None:
    """Direct only a complete, single-file translation request; compounds need planning."""
    if re.match(r"(?:请)?(?:不要|不用|无需|禁止|别|勿)", question.strip()):
        return None
    filename = r"(?P<name>[\w][\w.-]{0,239}\.(?:pdf|docx?|pptx?|xlsx?|md|markdown|txt))"
    section = r"(?:全文|整篇|整个文档|第[一二三四五六七八九十百零〇0-9]+[章节])?"
    patterns = (
        rf"(?:请)?(?:帮我)?(?:把|将)?\s*{filename}\s*{section}\s*"
        r"(?:翻译|译)(?:成|为|到)\s*(?:英文|英语|中文|汉语)"
        r"(?:\s*(?:并总结|并做总结|并进行总结|[，,]\s*译后总结))?[。.!！]?",
        rf"(?:please\s+)?translate\s+{filename}\s+"
        r"(?:(?:in\s+its\s+entirety|chapter\s+[0-9]+)\s+)?"
        r"(?:to|into)\s+(?:English|Chinese)(?:\s+and\s+summari[sz]e)?[.!]?",
    )
    for pattern in patterns:
        if match := re.fullmatch(pattern, question.strip(), re.I):
            name = match["name"]
            if len(re.findall(r"\.(?:pdf|docx?|pptx?|xlsx?|md|markdown|txt)", name, re.I)) == 1:
                return name
    return None


def summary_requested(question: str) -> bool:
    return bool(
        re.search(
            r"翻译.{0,12}(?:并)?总结|译后总结|translate.{0,30}summar"
            r"|\band\s+summari[sz]e\b",
            question,
            re.I,
        )
    )


def target_language(question: str) -> str:
    """Read explicit target cues; language names can also describe the source."""
    language = r"英文|英语|中文|汉语|English\b|Chinese\b"
    targets = rf"(?:{language})(?:\s*(?:和|或|、|及|/|\band\b|\bor\b)\s*(?:{language}))*"
    matches = re.findall(
        rf"(?:翻译|译)(?:成|为|到)?\s*({targets})|\b(?:to|into)\s+({targets})",
        question,
        re.I,
    )
    names = " ".join(part for match in matches for part in match)
    if "中译英" in question:
        names += " 英文"
    if "英译中" in question:
        names += " 中文"
    if not names.strip():
        names = " ".join(re.findall(rf"\bin\s+({targets})", question, re.I))
    english = bool(re.search(r"英文|英语|\bEnglish\b", names, re.I))
    chinese = bool(re.search(r"中文|汉语|\bChinese\b", names, re.I))
    if english == chinese:
        raise TranslationUnavailable("请明确译成中文还是英文。")
    return "英文" if english else "中文"


def section_key(marker: str) -> tuple[str, int] | None:
    if marker.startswith("第"):
        number, kind = marker[1:-1], marker[-1]
        if number.isdigit():
            return kind, int(number)
        if "十" in number:
            before, after = number.split("十", 1)
            tens = CHINESE_NUMBERS.get(before, 1) if before else 1
            units = CHINESE_NUMBERS.get(after, 0) if after else 0
            return kind, tens * 10 + units
        if number in CHINESE_NUMBERS:
            return kind, CHINESE_NUMBERS[number]
        return None
    kind, number = marker.casefold().split()
    return kind, int(number)


def select_section(
    elements: list[tuple[SourceUnit, ContentElement]], question: str
) -> list[tuple[SourceUnit, ContentElement]]:
    matches = SECTION.findall(question)
    if not matches:
        if re.search(
            r"章节|第[一二三四五六七八九十百零〇0-9]+部分|(?:的|指定|这一|该|某).{0,8}(?:章|节|部分)|\b(?:chapter|section)\b",
            question,
            re.I,
        ) and not re.search(r"全文|整篇|整个文档|\b(?:whole|entire)\b", question, re.I):
            raise TranslationUnavailable("请给出要翻译的章节编号，例如“第二章”。")
        return elements
    if len(set(matches)) != 1:
        raise TranslationUnavailable("请一次只指定一个章节。")
    wanted = matches[0].casefold()
    wanted_key = section_key(wanted)
    starts = [
        index
        for index, (_, element) in enumerate(elements)
        if element.element_type == "heading"
        and (
            wanted in (element.content_text or "").casefold()
            or any(
                section_key(marker) == wanted_key
                for marker in SECTION.findall(element.content_text or "")
            )
        )
    ]
    if starts:
        shallowest = min(
            (elements[index][1].structured_payload or {}).get("level") or 99 for index in starts
        )
        starts = [
            index
            for index in starts
            if ((elements[index][1].structured_payload or {}).get("level") or 99) == shallowest
        ]
    if len(starts) != 1:
        raise TranslationUnavailable("未能唯一定位该章节；请提供文档中的准确章节标题。")
    start = starts[0]
    heading = elements[start][1]
    level = (heading.structured_payload or {}).get("level")
    end = len(elements)
    for index in range(start + 1, len(elements)):
        candidate = elements[index][1]
        if candidate.element_type != "heading":
            continue
        other_level = (candidate.structured_payload or {}).get("level")
        if level is None or other_level is None or other_level <= level:
            end = index
            break
    return elements[start:end]


def split_parts(elements: list[tuple[SourceUnit, ContentElement]]) -> list[TranslationPart]:
    parts: list[TranslationPart] = []
    text = ""
    ids: list[int] = []
    label = ""

    def flush() -> None:
        nonlocal text, ids
        if text.strip():
            parts.append(TranslationPart(text, tuple(dict.fromkeys(ids)), label))
        text, ids = "", []

    for source, element in elements:
        value = element.content_text or ""
        if not value.strip() or (element.structured_payload or {}).get("text_origin") == "vision":
            continue
        source_label = source.label or f"{source.unit_type} {source.ordinal + 1}"
        if text and source_label != label:
            flush()
        label = source_label
        while value:
            remaining = MAX_PART_CHARS - len(text) - (1 if text else 0)
            if remaining <= 0:
                flush()
                remaining = MAX_PART_CHARS
            if text and remaining < len(value) <= MAX_PART_CHARS:
                flush()
                remaining = MAX_PART_CHARS
            take = value[:remaining]
            if len(value) > remaining:
                line_break = value.rfind("\n", 0, remaining)
                if line_break >= 0:
                    take = value[: line_break + 1]
            text += ("\n" if text else "") + take
            ids.append(element.id)
            value = value[len(take) :]
            if value:
                flush()
    flush()
    if not parts:
        raise TranslationUnavailable("该文档没有可逐字翻译的原文。")
    if len(parts) > MAX_PARTS:
        raise TranslationUnavailable("原文超过单次完整翻译的处理范围；请指定较小章节。")
    return parts


async def prepare_translation(
    repository: RetrievalRepository,
    identity: CurrentIdentity,
    scope: Scope,
    question: str,
) -> TranslationWork:
    language = target_language(question)
    if (scope.document_ids is None) == (scope.document_names is None):
        raise TranslationUnavailable("请指定一份文档的准确名称或 ID。")
    if scope.document_ids is not None and len(scope.document_ids) != 1:
        raise TranslationUnavailable("请一次只翻译一份文档。")
    if scope.document_names is not None and len(scope.document_names) != 1:
        raise TranslationUnavailable("请一次只翻译一份文档。")
    async with repository.sessions() as session, session.begin():
        query = (
            select(Document)
            .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
            .where(
                Document.deleted_at.is_(None),
                Document.active_revision_id.is_not(None),
                AuthorizationService.knowledge_scope(identity),
            )
        )
        if scope.document_ids is not None:
            query = query.where(Document.id == scope.document_ids[0])
        else:
            assert scope.document_names is not None
            query = query.where(Document.name_key == scope.document_names[0])
        documents = list((await session.scalars(query.limit(2))).all())
        if len(documents) != 1:
            raise TranslationUnavailable("文档不存在、无权访问或名称不唯一；请指定准确文档 ID。")
        document = documents[0]
        revision = await session.get(Revision, document.active_revision_id)
        if revision is None or revision.processing_status != "success":
            raise TranslationUnavailable("文档原文尚未完整处理，暂不能声明完整翻译。")
        units = list(
            (
                await session.scalars(
                    select(SourceUnit).where(SourceUnit.revision_id == revision.id)
                )
            ).all()
        )
        if any(unit.extraction_status not in {"success", "skipped"} for unit in units):
            raise TranslationUnavailable("文档存在未完成的原文提取，暂不能声明完整翻译。")
        rows = [
            (source, element)
            for source, element in (
                await session.execute(
                    select(SourceUnit, ContentElement)
                    .join(ContentElement, ContentElement.source_unit_id == SourceUnit.id)
                    .where(SourceUnit.revision_id == revision.id)
                    .order_by(SourceUnit.ordinal, SourceUnit.id, ContentElement.ordinal)
                )
            ).all()
        ]
        if any(
            source.extraction_status not in {"success", "skipped"} or element.status != "success"
            for source, element in rows
        ):
            raise TranslationUnavailable("文档存在未完成的原文提取，暂不能声明完整翻译。")
        selected = select_section(rows, question)
        parts = split_parts(selected)
        element_ids = {element_id for part in parts for element_id in part.element_ids}
        anchors = list(
            (
                await session.execute(
                    select(SourceAnchor.element_id, Chunk.id, SourceAnchor.id)
                    .join(ChunkAnchor, ChunkAnchor.anchor_id == SourceAnchor.id)
                    .join(Chunk, Chunk.id == ChunkAnchor.chunk_id)
                    .where(
                        SourceAnchor.element_id.in_(element_ids),
                        SourceAnchor.revision_id == revision.id,
                        Chunk.status == "ready",
                    )
                    .order_by(SourceAnchor.element_id, Chunk.id)
                )
            ).all()
        )
    indexed: dict[int, list[tuple[int, int]]] = {}
    for element_id, chunk_id, anchor_id in anchors:
        indexed.setdefault(element_id, []).append((chunk_id, anchor_id))
    allowed = await repository.load(identity, scope, list({chunk_id for _, chunk_id, _ in anchors}))
    verified: dict[int, list[tuple[int, Anchor]]] = {}
    for element_id, locations in indexed.items():
        for chunk_id, anchor_id in locations:
            if (chunk := allowed.get(chunk_id)) is None:
                continue
            verified.setdefault(element_id, []).extend(
                [
                    (chunk_id, anchor)
                    for anchor in chunk.anchors
                    if anchor.id == anchor_id and anchor.text_origin != "vision"
                ]
            )
    if any(element_id not in verified for element_id in element_ids):
        raise TranslationUnavailable("部分原文没有当前可验证的来源定位，无法完成整篇翻译。")
    attributed: list[TranslationPart] = []
    for index, part in enumerate(parts, 1):
        chosen = verified[part.element_ids[0]][0]
        chunk_id, anchor = chosen
        key = f"source_{index}"
        attributed.append(
            TranslationPart(
                part.text,
                part.element_ids,
                part.source_label,
                CitationCandidate(key, chunk_id, anchor.id, anchor.quote[:500]),
            )
        )
    return TranslationWork(document.name, language, tuple(attributed))
