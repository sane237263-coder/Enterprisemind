import pytest

from enterprise_mind.rag.evaluation import EvaluationCase, evaluate, ndcg


def test_metrics_count_permission_and_citation_violations_in_every_channel():
    case = EvaluationCase(
        relevant={"a": 2, "b": 1},
        keyword=("a", "hidden"),
        vector=("b", "a"),
        approximate=("a",),
        fused=("a", "b"),
        reranked=("a", "b"),
        context=("a",),
        allowed=frozenset({"a", "b"}),
        cited=("source1", "forged"),
        valid_citations=frozenset({"source1"}),
    )
    report = evaluate([case])
    assert report["keyword_recall_at_50"] == 0.5
    assert report["vector_recall_at_50"] == 1
    assert report["hnsw_recall_at_50"] == 0.5
    assert report["rerank_ndcg_at_10"] == 1
    assert report["permission_violations"] == report["invalid_citations"] == 1
    assert report["no_evidence_context_rate"] is None
    assert ndcg(("a", "a"), {"a": 2, "b": 1}) < 1
    with pytest.raises(ValueError):
        evaluate([])
