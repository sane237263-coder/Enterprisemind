"""Source-bound literal terms for document translation; never infer translated names."""

import json
import re
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from enterprise_mind.prompts import load_prompt

MAX_TERM_CHARS = 120
MAX_TERMS = 128
MAX_EXTRACTION_CHARS = 4096
TOKEN_PREFIX = "⟦EM_TERM_"
TOKEN = re.compile(r"⟦EM_TERM_[a-f0-9]{32}_[0-9]+⟧")
TERM_KINDS = {"organization", "person", "product"}
# Unambiguous corporate designators provide a source-only backstop when model NER
# omits a company or returns only its first word. Ordinary headings/record labels
# have no such designator and must remain translatable.
_DESIGNATOR = r"(?:Ltd\.?|Inc\.?|Corp\.?|LLC|LLP|PLC)"
_NAME_WORD = rf"(?!{_DESIGNATOR}(?![A-Za-z0-9_]))[A-Z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*"
_LEADING_CONTEXT = r"(?:At|By|From|For|To|With|Contact|Please|Within|Under|In|On|And|Or)"
CORPORATE_NAME = re.compile(
    rf"(?<![A-Za-z0-9_])(?!(?:{_LEADING_CONTEXT})\b)"
    rf"{_NAME_WORD}(?:[ \t]+(?:(?:of|the|and|&)[ \t]+)?{_NAME_WORD}){{0,7}}"
    rf"(?:,[ \t]*|[ \t]+){_DESIGNATOR}(?![A-Za-z0-9_])"
)


class InvalidTranslationTerms(ValueError):
    """A source term or a returned literal marker could not be verified."""


class TranslationModel(Protocol):
    def answer(
        self, messages: list[BaseMessage], *, json_mode: bool = False
    ) -> AsyncIterator[str]: ...


def source_terms(response: str, source: str) -> tuple[str, ...]:
    """Accept only bounded, exact source substrings, with no model-created glossary."""
    try:
        value = json.loads(response)
    except (ValueError, TypeError):
        raise InvalidTranslationTerms from None
    if not isinstance(value, dict) or set(value) != {"terms"}:
        raise InvalidTranslationTerms
    entries = value["terms"]
    if not isinstance(entries, list) or len(entries) > MAX_TERMS:
        raise InvalidTranslationTerms
    terms: list[str] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"text", "kind"}
            or not isinstance(entry["kind"], str)
            or entry["kind"] not in TERM_KINDS
        ):
            raise InvalidTranslationTerms
        term = entry["text"]
        if (
            not isinstance(term, str)
            or not 1 <= len(term) <= MAX_TERM_CHARS
            or term.strip() != term
            or any(char in term for char in "\n\r\t")
            or term not in source
            or TOKEN_PREFIX in term
        ):
            raise InvalidTranslationTerms
        terms.append(term)
    if len(terms) != len(set(terms)):
        raise InvalidTranslationTerms
    return tuple(terms)


def source_lines(source: str) -> list[str]:
    """Keep each nonblank source line separate through model generation."""
    return [line for line in source.splitlines() if line.strip()]


def translated_lines(response: str, expected: list[str]) -> list[str]:
    """Require one nonempty translation per source line before publishing anything."""
    try:
        value = json.loads(response)
    except (ValueError, TypeError):
        raise InvalidTranslationTerms from None
    if not isinstance(value, dict) or set(value) != {"lines"}:
        raise InvalidTranslationTerms
    lines = value["lines"]
    if (
        not isinstance(lines, list)
        or len(lines) != len(expected)
        or any(not isinstance(line, str) or not line.strip() for line in lines)
    ):
        raise InvalidTranslationTerms
    return lines


@dataclass(frozen=True)
class ProtectedTranslation:
    parts: tuple[str, ...]
    literals: tuple[tuple[str, str], ...]

    def restore(self, translated: str, source: str, *, summary: bool = False) -> str:
        """Reject missing/extra/foreign markers before restoring their exact source bytes."""
        expected = Counter(TOKEN.findall(source))
        actual = Counter(TOKEN.findall(translated))
        allowed = dict(self.literals)
        if any(token not in allowed for token in actual):
            raise InvalidTranslationTerms
        if (not summary and actual != expected) or (summary and set(actual) - set(expected)):
            raise InvalidTranslationTerms
        # Malformed markers must not leak to users or survive partial replacement.
        if TOKEN_PREFIX in TOKEN.sub("", translated):
            raise InvalidTranslationTerms
        return TOKEN.sub(lambda match: allowed[match.group()], translated)


def protect_parts(parts: list[str], terms: tuple[str, ...]) -> ProtectedTranslation:
    if any(TOKEN_PREFIX in part for part in parts):
        raise InvalidTranslationTerms
    terms = tuple(
        dict.fromkeys(
            [*terms, *(match.group() for part in parts for match in CORPORATE_NAME.finditer(part))]
        )
    )
    if len(terms) > MAX_TERMS or any(len(term) > MAX_TERM_CHARS for term in terms):
        raise InvalidTranslationTerms
    if not terms:
        return ProtectedTranslation(tuple(parts), ())
    # Longest match avoids splitting a full company name into a shorter alias.
    ordered = sorted(set(terms), key=lambda term: (-len(term), term))
    nonce = uuid4().hex
    mapping = {term: f"{TOKEN_PREFIX}{nonce}_{index}⟧" for index, term in enumerate(ordered)}

    # Latin names must not corrupt containing words (Ann / Annual, Atlas / Atlas2).
    def literal(term: str) -> str:
        left = r"(?<![A-Za-z0-9_])" if term[0].isascii() and term[0].isalnum() else ""
        right = r"(?![A-Za-z0-9_])" if term[-1].isascii() and term[-1].isalnum() else ""
        return left + re.escape(term) + right

    pattern = re.compile("|".join(literal(term) for term in ordered))
    masked = tuple(pattern.sub(lambda match: mapping[match.group()], part) for part in parts)
    return ProtectedTranslation(masked, tuple((token, term) for term, token in mapping.items()))


async def prepare_terms(model: TranslationModel, parts: list[str]) -> ProtectedTranslation:
    """Extract in bounded batches from already authorized, selected source text only."""
    batches: list[str] = []
    current = ""
    for part in parts:
        if len(part) > MAX_EXTRACTION_CHARS:
            raise InvalidTranslationTerms
        if current and len(current) + len(part) + 1 > MAX_EXTRACTION_CHARS:
            batches.append(current)
            current = ""
        current += ("\n" if current else "") + part
    if current:
        batches.append(current)
    terms: dict[str, None] = {}
    for source in batches:
        messages = [
            SystemMessage(load_prompt("agent/translation_terms")),
            HumanMessage(json.dumps({"source_text": source}, ensure_ascii=False)),
        ]
        response = "".join(
            [piece async for piece in model.answer(messages, json_mode=True)]
        ).strip()
        terms.update(dict.fromkeys(source_terms(response, source)))
        if len(terms) > MAX_TERMS:
            raise InvalidTranslationTerms
    return protect_parts(parts, tuple(terms))
