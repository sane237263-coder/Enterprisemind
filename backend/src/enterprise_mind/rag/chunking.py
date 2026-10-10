"""Structure-first chunking. All quote ranges remain exact substrings of source text."""

import ast
import csv
import io
import re
from dataclasses import dataclass, field, replace
from typing import Any

from enterprise_mind.ingestion.parsing import column_name
from enterprise_mind.rag.config import ChunkConfig
from enterprise_mind.rag.providers import TokenCounter


class ChunkingFailure(Exception):
    pass


@dataclass(frozen=True)
class AnchorInput:
    id: int
    source_unit_id: int
    element_id: int
    locator_type: str
    locator: dict[str, Any]
    text: str


@dataclass(frozen=True)
class ElementInput:
    id: int
    parent_id: int | None
    ordinal: int
    kind: str
    text: str
    payload: dict[str, Any]
    anchor: AnchorInput


@dataclass(frozen=True)
class Piece:
    anchor: AnchorInput
    quote: str
    locator: dict[str, Any]
    start: int
    end: int


@dataclass
class ChunkDraft:
    kind: str
    content: str
    token_count: int
    context_group: str | None
    metadata: dict[str, Any]
    pieces: list[Piece] = field(default_factory=list)


def slice_locator(anchor: AnchorInput, start: int, end: int) -> dict[str, Any]:
    locator = dict(anchor.locator)
    if anchor.locator_type == "text":
        origin = int(locator["char_start"])
        line = int(locator["start_line"])
        locator.update(
            char_start=origin + start,
            char_end=origin + end,
            start_line=line + anchor.text[:start].count("\n"),
            end_line=line + anchor.text[:end].rstrip("\n").count("\n"),
        )
    elif anchor.locator_type in {"docx", "pptx"} and "char_start" in locator:
        locator.update(
            char_start=int(locator["char_start"]) + start, char_end=int(locator["char_start"]) + end
        )
    return locator


def text_spans(
    value: str, prefix: str, config: ChunkConfig, counter: TokenCounter
) -> list[tuple[int, int]]:
    spans = []
    if counter.count(prefix + value) <= config.max_tokens:
        return [(0, len(value))]
    # Only inspect a bounded window after the first whole-structure check. Recounting the
    # entire remaining source for every chunk makes long text processing quadratic.
    window_chars = max(1024, config.max_tokens * 16)
    start = 0
    while start < len(value):
        window_end = min(len(value), start + window_chars)
        if (
            window_end == len(value)
            and counter.count(prefix + value[start:window_end]) <= config.max_tokens
        ):
            spans.append((start, window_end))
            break
        low, high = start + 1, window_end
        while low < high:
            mid = (low + high + 1) // 2
            if counter.count(prefix + value[start:mid]) <= config.max_tokens:
                low = mid
            else:
                high = mid - 1
        end = low
        boundaries = list(re.finditer(r"[\n。！？.!?;；]+\s*|\s+", value[start:end]))
        if boundaries and boundaries[-1].end() > (end - start) // 2:
            end = start + boundaries[-1].end()
        if end <= start or counter.count(prefix + value[start:end]) > config.max_tokens:
            raise ChunkingFailure("CHUNK_STRUCTURE_TOO_LARGE")
        spans.append((start, end))
        overlap_start = end
        if config.overlap_tokens:
            lo, hi = start + 1, end
            while lo < hi:
                mid = (lo + hi) // 2
                if counter.count(value[mid:end]) <= config.overlap_tokens:
                    hi = mid
                else:
                    lo = mid + 1
            overlap_start = lo
        start = max(start + 1, overlap_start)
    return spans


def table_spans(element: ElementInput) -> tuple[tuple[int, int], list[tuple[int, int]]]:
    value = element.anchor.text
    lines = value.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    if element.anchor.locator_type == "xlsx" or element.payload.get("serialization") == "csv":
        try:
            dialect = csv.Sniffer().sniff(value[:8192], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(value, newline=""), dialect=dialect, strict=True)
        spans = []
        start = 0
        for _ in reader:
            end = offsets[reader.line_num]
            spans.append((start, end))
            start = end
        if not spans:
            raise ChunkingFailure("CHUNK_SOURCE_INVALID")
        return spans[0], spans[1:]
    if len(lines) < 2:
        raise ChunkingFailure("CHUNK_SOURCE_INVALID")
    return (0, offsets[2]), [(offsets[i], offsets[i + 1]) for i in range(2, len(lines))]


def make_draft(
    element: ElementInput,
    prefix: str,
    spans: list[tuple[int, int]],
    kind: str,
    group: str | None,
    metadata: dict[str, Any],
    counter: TokenCounter,
    locators: list[dict[str, Any]] | None = None,
) -> ChunkDraft:
    content = prefix
    pieces: list[Piece] = []
    for index, (start, end) in enumerate(spans):
        quote = element.anchor.text[start:end]
        if not quote:
            continue
        if pieces and not content.endswith("\n"):
            content += "\n"
        at = len(content)
        content += quote
        pieces.append(
            Piece(
                element.anchor,
                quote,
                locators[index] if locators else slice_locator(element.anchor, start, end),
                at,
                len(content),
            )
        )
    return ChunkDraft(kind, content, counter.count(content), group, metadata, pieces)


def build_source(
    elements: list[ElementInput], config: ChunkConfig, counter: TokenCounter
) -> list[ChunkDraft]:
    by_id = {element.id: element for element in elements}
    output: list[ChunkDraft] = []
    previous: ElementInput | None = None
    for element in sorted(elements, key=lambda item: item.ordinal):
        if not element.anchor.text.strip():
            continue
        headings = []
        parent = by_id.get(element.parent_id or -1)
        seen = {element.id}
        while parent is not None:
            if parent.id in seen:
                raise ChunkingFailure("CHUNK_SOURCE_INVALID")
            seen.add(parent.id)
            if parent.kind == "heading":
                headings.append(parent.text)
            parent = by_id.get(parent.parent_id or -1)
        headings.reverse()
        prefix = ("[标题路径] " + " / ".join(headings) + "\n") if headings else ""
        metadata: dict[str, Any] = {"heading_path": headings, "element_types": [element.kind]}
        if element.payload.get("text_origin"):
            metadata["text_origin"] = element.payload["text_origin"]
        if element.payload.get("uncertainties"):
            metadata["uncertainties"] = element.payload["uncertainties"]
        group = f"element:{element.id}"
        kind = element.kind if element.kind in {"table", "code", "image"} else "text"
        if kind == "table":
            headers, rows = table_spans(element)
            merged = element.payload.get("merged_ranges", [])
            merged_rows = []
            for cell_range in merged:
                match = re.fullmatch(r"[A-Z]+([1-9][0-9]*):[A-Z]+([1-9][0-9]*)", cell_range)
                if not match:
                    raise ChunkingFailure("CHUNK_SOURCE_INVALID")
                merged_rows.append((int(match[1]), int(match[2])))
            header_count = 1
            while any(start <= header_count < end for start, end in merged_rows):
                header_count = max(end for start, end in merged_rows if start <= header_count)
            if header_count > len(rows) + 1:
                raise ChunkingFailure("CHUNK_SOURCE_INVALID")
            if header_count > 1:
                headers = (headers[0], rows[header_count - 2][1])
                rows = rows[header_count - 1 :]
            metadata.update(merged_ranges=merged, header_rows=header_count)
            metadata.update(
                {
                    key: element.payload[key]
                    for key in ("table_name", "sheet")
                    if key in element.payload
                }
            )
            width = max(
                [
                    len(element.payload.get("headers", [])),
                    *[len(row) for row in element.payload.get("rows", [])],
                ]
            )
            batches: list[tuple[int, int]] = []
            first = 0
            while first < len(rows):
                last = first
                while last < len(rows):
                    finish = last + 1
                    while any(start <= header_count + finish < end for start, end in merged_rows):
                        finish += 1
                    if finish > len(rows):
                        raise ChunkingFailure("CHUNK_SOURCE_INVALID")
                    candidate = (
                        prefix
                        + element.anchor.text[slice(*headers)]
                        + element.anchor.text[rows[first][0] : rows[finish - 1][1]]
                    )
                    if counter.count(candidate) > config.max_tokens:
                        break
                    last = finish
                    if counter.count(candidate) >= config.target_tokens:
                        break
                if last == first:
                    raise ChunkingFailure("CHUNK_STRUCTURE_TOO_LARGE")
                batches.append((first, last))
                first = last
            for first, last in batches or [(0, 0)]:
                spans = [headers] + ([(rows[first][0], rows[last - 1][1])] if last else [])
                locators = [slice_locator(element.anchor, *span) for span in spans]
                if element.anchor.locator_type == "xlsx":
                    locators[0]["cell_range"] = f"A1:{column_name(width)}{header_count}"
                    if last:
                        locators[1]["cell_range"] = (
                            f"A{first + header_count + 1}:{column_name(width)}{last + header_count}"
                        )
                output.append(
                    make_draft(element, prefix, spans, kind, group, metadata, counter, locators)
                )
        elif kind == "code":
            language = element.payload.get("language", "plain_text")
            metadata["language"] = language
            prefix += f"[代码语言] {language}\n"
            spans = [(0, len(element.anchor.text))]
            if counter.count(prefix + element.anchor.text) > config.max_tokens:
                # Syntax-aware splitting for Python. Unknown syntax stays indivisible.
                if language not in {"python", "py"} or element.text not in element.anchor.text:
                    raise ChunkingFailure("CHUNK_STRUCTURE_TOO_LARGE")
                try:
                    tree = ast.parse(element.text)
                except SyntaxError:
                    raise ChunkingFailure("CHUNK_STRUCTURE_TOO_LARGE") from None
                lines = element.text.splitlines(keepends=True)
                offset = element.anchor.text.index(element.text)
                starts = [
                    offset
                    + sum(
                        map(
                            len,
                            lines[
                                : min(
                                    [
                                        node.lineno,
                                        *[d.lineno for d in getattr(node, "decorator_list", [])],
                                    ]
                                )
                                - 1
                            ],
                        )
                    )
                    for node in tree.body
                ]
                boundaries = sorted(set([0, *starts, len(element.anchor.text)]))
                spans = list(zip(boundaries[:-1], boundaries[1:], strict=True))
            for span in spans:
                output.append(make_draft(element, prefix, [span], kind, group, metadata, counter))
            if (
                previous
                and previous.kind == "paragraph"
                and previous.parent_id == element.parent_id
                and previous.ordinal + 1 == element.ordinal
            ):
                for draft in output:
                    if any(piece.anchor.element_id == previous.id for piece in draft.pieces):
                        draft.context_group = group
        else:
            spans = text_spans(element.anchor.text, prefix, config, counter)
            drafts = [
                make_draft(
                    element,
                    prefix,
                    [span],
                    kind,
                    group if len(spans) > 1 else None,
                    metadata,
                    counter,
                )
                for span in spans
            ]
            if (
                len(drafts) == 1
                and output
                and previous
                and kind == "text"
                and element.kind != "heading"
                and previous.kind != "heading"
                and previous.parent_id == element.parent_id
                and output[-1].kind == "text"
                and output[-1].context_group is None
                and len(spans) == 1
                and output[-1].token_count < config.min_tokens
            ):
                candidate = output[-1].content + "\n" + drafts[0].content[len(prefix) :]
                if counter.count(candidate) <= config.target_tokens:
                    delta = len(output[-1].content) + 1 - len(prefix)
                    output[-1].pieces.extend(
                        replace(piece, start=piece.start + delta, end=piece.end + delta)
                        for piece in drafts[0].pieces
                    )
                    output[-1].content = candidate
                    output[-1].token_count = counter.count(candidate)
                    drafts = []
            output.extend(drafts)
        previous = element
    if any(draft.token_count > config.max_tokens or not draft.pieces for draft in output):
        raise ChunkingFailure("CHUNK_STRUCTURE_TOO_LARGE")
    return output
