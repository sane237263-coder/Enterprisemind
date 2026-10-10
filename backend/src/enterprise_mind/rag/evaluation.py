"""Offline aggregate metrics; this module never collects live queries or model inputs."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class EvaluationCase:
    relevant: dict[str, int]
    keyword: tuple[str, ...]
    vector: tuple[str, ...]
    approximate: tuple[str, ...]
    fused: tuple[str, ...]
    reranked: tuple[str, ...]
    context: tuple[str, ...]
    allowed: frozenset[str]
    cited: tuple[str, ...]
    valid_citations: frozenset[str]


def recall(ranked: tuple[str, ...], relevant: set[str], limit: int) -> float:
    return len(set(ranked[:limit]) & relevant) / len(relevant) if relevant else 0.0


def ndcg(ranked: tuple[str, ...], relevant: dict[str, int], limit: int = 10) -> float:
    def gain(grades: list[int]) -> float:
        return float(
            sum((2**grade - 1) / math.log2(index + 2) for index, grade in enumerate(grades))
        )

    ideal = gain(sorted(relevant.values(), reverse=True)[:limit])
    if not ideal:
        return 0.0
    # Duplicate IDs receive no second credit.
    seen = set()
    grades = []
    for item in ranked[:limit]:
        grades.append(relevant.get(item, 0) if item not in seen else 0)
        seen.add(item)
    return gain(grades) / ideal


def evaluate(cases: list[EvaluationCase]) -> dict[str, float | int | None]:
    if not cases:
        raise ValueError("evaluation set is empty")
    positives = [case for case in cases if any(grade > 0 for grade in case.relevant.values())]
    negatives = [case for case in cases if not any(grade > 0 for grade in case.relevant.values())]

    def mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    def recall_values(channel: str, limit: int) -> list[float]:
        return [
            recall(
                getattr(case, channel),
                {key for key, grade in case.relevant.items() if grade > 0},
                limit,
            )
            for case in positives
        ]

    violations = sum(
        len(
            (
                set(case.keyword)
                | set(case.vector)
                | set(case.approximate)
                | set(case.fused)
                | set(case.reranked)
                | set(case.context)
            )
            - case.allowed
        )
        for case in cases
    )
    return {
        "cases": len(cases),
        "positive_cases": len(positives),
        "negative_cases": len(negatives),
        "keyword_recall_at_50": mean(recall_values("keyword", 50)),
        "vector_recall_at_50": mean(recall_values("vector", 50)),
        "hnsw_recall_at_50": mean(
            [
                recall(case.approximate, set(case.vector[:50]), 50)
                for case in positives
                if case.vector
            ]
        ),
        "rrf_recall_at_30": mean(recall_values("fused", 30)),
        "rerank_ndcg_at_10": mean([ndcg(case.reranked, case.relevant) for case in positives]),
        "context_recall": mean(recall_values("context", 10)),
        "no_evidence_context_rate": mean([float(bool(case.context)) for case in negatives]),
        "permission_violations": violations,
        "invalid_citations": sum(len(set(case.cited) - case.valid_citations) for case in cases),
    }
