"""Evaluate explicitly supplied offline judgments and ID lists; output aggregate metrics only."""

import argparse
import json
from pathlib import Path

from enterprise_mind.rag.evaluation import EvaluationCase, evaluate

parser = argparse.ArgumentParser()
parser.add_argument("input", type=Path, help="Locally curated JSONL; no automatic query collection")
args = parser.parse_args()
cases = []
try:
    for line in args.input.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        required = {
            "relevant",
            "keyword",
            "vector",
            "approximate",
            "fused",
            "reranked",
            "context",
            "allowed",
            "cited",
            "valid_citations",
        }
        if set(item) != required or not isinstance(item["relevant"], dict):
            raise ValueError("invalid case")
        if any(
            not isinstance(key, str) or type(value) is not int or value not in (0, 1, 2)
            for key, value in item["relevant"].items()
        ):
            raise ValueError("invalid judgments")
        for key in required - {"relevant"}:
            if not isinstance(item[key], list) or any(
                not isinstance(value, str) for value in item[key]
            ):
                raise ValueError("invalid candidate IDs")
        cases.append(
            EvaluationCase(
                relevant=item["relevant"],
                **{
                    key: tuple(item[key])
                    for key in (
                        "keyword",
                        "vector",
                        "approximate",
                        "fused",
                        "reranked",
                        "context",
                        "cited",
                    )
                },
                allowed=frozenset(item["allowed"]),
                valid_citations=frozenset(item["valid_citations"]),
            )
        )
    print(json.dumps(evaluate(cases), ensure_ascii=False, indent=2, allow_nan=False))
except (OSError, UnicodeError, ValueError, KeyError, TypeError):
    parser.error("评测文件格式无效、为空或不可读取；未输出文件内容。")
