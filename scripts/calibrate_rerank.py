"""Development-only threshold screening with a separate validation split; no config writes."""

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from enterprise_mind.rag.config import EndpointConfig, ProvidersConfig
from enterprise_mind.rag.providers import JSONProvider


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/providers.example.yaml"))
    parser.add_argument(
        "--dataset", type=Path, default=Path("backend/tests/fixtures/rerank_synthetic.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path(".local/model-checks/rerank_calibration.json")
    )
    args = parser.parse_args()
    source = args.config.resolve()
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    values = dict(raw["rerank"])
    endpoint = urlsplit(values["base_url"])
    origins = ProvidersConfig(allowed_origins=raw["allowed_origins"]).allowed_origins
    if (
        values.get("protocol") != "jina_compatible"
        or f"{endpoint.scheme}://{endpoint.netloc}" not in origins
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("invalid calibration endpoint")
    values["tokenizer_file"] = str((source.parent / values["tokenizer_file"]).resolve())
    values["max_retries"] = 0
    config = EndpointConfig.model_validate(
        {key: values[key] for key in EndpointConfig.model_fields if key in values}
    )
    provider = JSONProvider(config, values["secret_file_env"])
    dataset_bytes = args.dataset.read_bytes()
    dataset = json.loads(dataset_bytes)
    cases = dataset["cases"]
    assert len(cases) == len({case["id"] for case in cases})
    report = {
        "model_key": config.model_key,
        "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "scope": "synthetic_development_only",
        "cases": [],
    }
    for case in cases:
        assert case["split"] in {"calibration", "validation"}
        assert all(type(i) is int and 0 <= i < len(case["documents"]) for i in case["relevant"])
        assert all(
            provider.counter.count(case["query"] + "\n" + doc) <= config.max_input_tokens
            for doc in case["documents"]
        )
        response = await provider.request(
            "/rerank",
            {
                "model": config.model,
                "query": case["query"],
                "documents": case["documents"],
                "top_n": len(case["documents"]),
                "return_documents": False,
            },
        )
        results = response["results"]
        assert len(results) == len(case["documents"])
        assert all(type(row["index"]) is int for row in results)
        assert sorted(row["index"] for row in results) == list(range(len(results)))
        scores = [row["relevance_score"] for row in sorted(results, key=lambda row: row["index"])]
        assert all(type(score) in (float, int) and math.isfinite(score) for score in scores)
        report["cases"].append(
            {
                "id": case["id"],
                "split": case["split"],
                "relevant": case["relevant"],
                "scores": scores,
            }
        )
        print(case["id"] + ": scored", flush=True)
    calibration = [
        (score, i in case["relevant"])
        for case in report["cases"]
        if case["split"] == "calibration"
        for i, score in enumerate(case["scores"])
    ]
    highest_negative = max(score for score, relevant in calibration if not relevant)
    eligible = [score for score, relevant in calibration if relevant and score > highest_negative]
    threshold = (
        (highest_negative + min(eligible)) / 2
        if eligible
        else math.nextafter(highest_negative, math.inf)
    )
    metrics = {}
    for split in ("calibration", "validation"):
        pairs = [
            (score, i in case["relevant"])
            for case in report["cases"]
            if case["split"] == split
            for i, score in enumerate(case["scores"])
        ]
        positives = sum(relevant for _, relevant in pairs)
        true_positive = sum(score >= threshold and relevant for score, relevant in pairs)
        false_positive = sum(score >= threshold and not relevant for score, relevant in pairs)
        metrics[split] = {
            "pairs": len(pairs),
            "positives": positives,
            "true_positive": true_positive,
            "false_positive": false_positive,
            "recall": true_positive / positives,
        }
    scores = [score for case in report["cases"] for score in case["scores"]]
    report.update(
        candidate_threshold=threshold,
        observed_score_range=[min(scores), max(scores)],
        metrics=metrics,
        development_gate_passed=metrics["validation"]["false_positive"] == 0
        and metrics["validation"]["recall"] >= 0.8,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "cases"}, ensure_ascii=True
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as error:
        print("Calibration failed: " + type(error).__name__ + "; no configuration was changed.")
        raise SystemExit(1) from None
