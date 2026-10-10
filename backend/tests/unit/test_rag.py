import hashlib
from dataclasses import replace

import pytest
from pydantic import ValidationError

from enterprise_mind.infrastructure.settings import WorkerConfig
from enterprise_mind.ingestion.parsing import parse_csv, parse_text
from enterprise_mind.rag.chunking import (
    AnchorInput,
    ChunkingFailure,
    ElementInput,
    build_source,
    text_spans,
)
from enterprise_mind.rag.config import (
    ChunkConfig,
    ProvidersConfig,
    RAGConfig,
    RAGSettings,
    load_rag,
)
from enterprise_mind.rag.keywords import query_expression, search_tokens
from enterprise_mind.rag.providers import ProviderFailure, validate_vector
from enterprise_mind.rag.repository import Anchor, Candidate, Scope
from enterprise_mind.rag.retrieval import fuse, overlap, strong_evidence


class CharacterCounter:
    """Test fixture only; never selected by application configuration."""

    def count(self, text):
        return len(text)


def inputs(source):
    return [
        ElementInput(
            i + 1,
            e.parent + 1 if e.parent is not None else None,
            i,
            e.kind,
            e.text,
            e.payload,
            AnchorInput(i + 1, 1, i + 1, e.locator_type, e.locator, e.source_text),
        )
        for i, e in enumerate(source.elements)
    ]


def check_quotes(drafts):
    for draft in drafts:
        for piece in draft.pieces:
            assert draft.content[piece.start : piece.end] == piece.quote
            assert piece.quote in piece.anchor.text


def test_config_preserves_unconfigured_models():
    rag, providers = load_rag(RAGSettings())
    assert providers.embedding is providers.rerank is None
    assert rag.rerank.min_score is rag.rerank.model_key is None
    assert rag.chunk.max_tokens == 600
    with pytest.raises(ValidationError):
        ChunkConfig(overlap_tokens=600)
    with pytest.raises(ValidationError):
        RAGConfig(context={"max_blocks": 11})
    with pytest.raises(ValidationError):
        RAGConfig(rerank={"min_score": 0.5})
    with pytest.raises(ValidationError):
        ProvidersConfig(allowed_origins=["https://user:password@example.test"])


def test_unicode_identifiers_and_query_syntax():
    terms = search_tokens("用户登录 getUserID snake_case ABC-123 v1.2.3 ＡＢＣ")
    for value in (
        "用户",
        "登录",
        "getuserid",
        "user",
        "snake_case",
        "snake",
        "abc-123",
        "v1.2.3",
        "abc",
    ):
        assert value in terms.split()
    assert query_expression("x' | !secret") == "'x' | 'secret'"


@pytest.mark.parametrize(
    "vector",
    [
        [],
        [0.0, 0.0],
        [float("nan"), 1.0],
        [float("inf"), 1.0],
        [True, 1.0],
        [1e99, 1.0],
        pytest.param([10**1000, 1.0], id="integer-overflow"),
    ],
)
def test_invalid_vectors_rejected(vector):
    with pytest.raises(ProviderFailure):
        validate_vector(vector, 2)


def test_structure_overlap_unicode_and_code_boundaries():
    raw = (
        "# 标题一\n\n"
        + "甲乙丙丁内容。" * 60
        + "\n\n# 标题二\n\n```python\ndef f():\n    return 1\n```\n"
    )
    source = parse_text(raw, True)
    drafts = build_source(
        inputs(source),
        ChunkConfig(target_tokens=100, max_tokens=120, min_tokens=20, overlap_tokens=15),
        CharacterCounter(),
    )
    check_quotes(drafts)
    assert all(d.token_count <= 120 for d in drafts)
    assert any(d.kind == "code" and "def f():" in d.content for d in drafts)
    for draft in drafts:
        for piece in draft.pieces:
            assert raw[piece.locator["char_start"] : piece.locator["char_end"]] == piece.quote
    groups = [d for d in drafts if d.context_group and d.kind == "text"]
    assert len(groups) > 2
    assert len({d.context_group for d in groups}) == 1


def test_long_text_tokenization_work_is_bounded_per_chunk():
    counted_lengths = []

    class MeasuringCounter:
        def count(self, value):
            counted_lengths.append(len(value))
            return len(value)

    raw = "长段落包含可检索内容。" * 6000
    config = ChunkConfig(target_tokens=100, max_tokens=120, min_tokens=20, overlap_tokens=15)
    spans = text_spans(raw, "", config, MeasuringCounter())
    assert spans[0][0] == 0 and spans[-1][1] == len(raw)
    assert all(end - start <= 120 for start, end in spans)
    assert all(right[0] <= left[1] for left, right in zip(spans, spans[1:], strict=False))
    assert sum(counted_lengths) < len(raw) * 60


def test_csv_rows_preserve_headers_multiline_cells_and_precise_ranges():
    raw = '姓名,说明,数值\n甲,"跨行\n说明",123\n乙,测试,456\n丙,其他,789\n'
    source = parse_csv(raw, WorkerConfig())
    drafts = build_source(
        inputs(source),
        ChunkConfig(target_tokens=28, max_tokens=35, min_tokens=5, overlap_tokens=2),
        CharacterCounter(),
    )
    assert len(drafts) >= 2
    check_quotes(drafts)
    assert all(d.content.startswith("姓名,说明,数值\n") for d in drafts)
    assert all(d.pieces[0].locator["cell_range"] == "A1:C1" for d in drafts)
    assert "跨行\n说明" in drafts[0].content
    assert drafts[0].pieces[1].locator["cell_range"] == "A2:C3"


def test_indivisible_oversized_code_fails_without_truncating():
    source = parse_text("```plain_text\n" + "x" * 100 + "\n```\n", True)
    with pytest.raises(ChunkingFailure, match="CHUNK_STRUCTURE_TOO_LARGE"):
        build_source(
            inputs(source),
            ChunkConfig(target_tokens=30, max_tokens=40, min_tokens=5, overlap_tokens=2),
            CharacterCounter(),
        )


def candidate(key, text, source=1, document=1):
    anchor = Anchor(key, source, 0, 0, "text", {"char_start": 0}, text, 0, len(text))
    return Candidate(key, document, 1, key, "file", "file", text, len(text), None, [anchor])


def test_rrf_uses_ranks_scope_intersection_and_strong_evidence():
    a, b = candidate(1, "ABC-123 支持加密"), candidate(2, "无关内容", source=2)
    cfg = RAGConfig()
    result = fuse({"keyword": [(1, 0.01), (2, 999)], "vector": [(2, 0), (1, 1)]}, {1: a, 2: b}, cfg)
    assert result[0].fused_score == pytest.approx(1 / 61 + 1 / 62)
    assert strong_evidence(a, "ABC-123", 1)
    assert not strong_evidence(b, "一般问题", 1)
    assert Scope(document_ids=(1, 2)).intersect(Scope(document_ids=(2, 3))).document_ids == (2,)
    assert Scope(document_ids=(1,)).intersect(Scope(document_ids=(2,))).document_ids == ()


def test_repeated_table_header_does_not_deduplicate_unrelated_body():
    a = candidate(1, "表头")
    b = candidate(2, "表头")
    a.anchors.append(replace(a.anchors[0], id=3, quote="甲方销售收入12345"))
    b.anchors.append(replace(b.anchors[0], id=4, quote="乙类采购成本67890"))
    assert overlap(a, b) < 0.8
    assert overlap(a, replace(a, id=99)) == 1.0
    assert overlap(a, replace(a, document_id=2)) == 0.0


def test_local_tokenizer_hash_and_no_truncation(tmp_path):
    from tokenizers import Tokenizer, models, pre_tokenizers

    from enterprise_mind.infrastructure.settings import ConfigurationError
    from enterprise_mind.rag.providers import LocalTokenizer

    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "one": 1}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.enable_truncation(max_length=1)
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    adapter = LocalTokenizer(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert adapter.count("one one one") == 3
    with pytest.raises(ConfigurationError):
        LocalTokenizer(path, "0" * 64)
