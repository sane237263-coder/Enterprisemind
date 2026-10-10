from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT.parent / "scripts" / "phase10_rag_eval.py"
SPEC = importlib.util.spec_from_file_location("phase10_rag_eval", SCRIPT)
assert SPEC and SPEC.loader
phase10_rag_eval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(phase10_rag_eval)


def test_synthetic_manifest_has_stable_hash_and_is_not_enterprise_gate():
    dataset, results = phase10_rag_eval.build_synthetic()

    assert dataset["dataset_sha256"] == phase10_rag_eval.canonical_cases_sha256(dataset["cases"])
    validation = phase10_rag_eval.validate_dataset(dataset)
    assert validation["valid"]
    assert validation["enterprise_dataset_gate"] == "blocked"

    result_map = {item["case_id"]: item for item in results}
    result_errors = []
    report = phase10_rag_eval.score_dataset(dataset, result_map, result_errors)
    assert report["execution"]["completion_denominator"] == 4
    assert report["execution"]["completed"] == 2
    assert report["execution"]["failed"] == 1
    assert report["execution"]["blocked"] == 1
    assert report["gates"]["overall"] == "blocked"
    assert report["human_review"]["pending_or_missing"] == 4


def test_dataset_rejects_duplicate_question_and_tampered_hash():
    dataset, _ = phase10_rag_eval.build_synthetic()
    dataset["cases"][1]["question"] = dataset["cases"][0]["question"]
    dataset["dataset_sha256"] = "0" * 64

    report = phase10_rag_eval.validate_dataset(dataset)

    assert not report["valid"]
    assert "duplicate_questions" in report["errors"]
    assert "dataset_sha256_mismatch" in report["errors"]


def test_missing_results_stay_in_completion_denominator():
    dataset, _ = phase10_rag_eval.build_synthetic()
    result = {
        "case_id": "synthetic-p10-005",
        "status": "completed",
        "observed_behavior": "refuse",
        "ranked_source_ids": [],
        "cited_source_ids": [],
        "valid_citation_source_ids": [],
        "authorized_source_ids": [],
        "answer_text": "没有找到足够依据。",
    }

    report = phase10_rag_eval.score_dataset(dataset, {result["case_id"]: result}, [])

    assert report["execution"]["completion_denominator"] == 4
    assert report["execution"]["completed"] == 1
    assert report["execution"]["not_executed"] == 3
    assert report["execution"]["completion_rate"] == pytest.approx(0.25)


def test_enterprise_gate_requires_200_valid_frozen_cases():
    source, _ = phase10_rag_eval.build_synthetic()
    cases = []
    for number in range(199):
        case = deepcopy(source["cases"][number % len(source["cases"])])
        case["case_id"] = f"enterprise-{number:03d}"
        case["dedup_key"] = f"enterprise-family-{number:03d}"
        case["question"] = f"授权企业问题 {number:03d}？"
        case["split"] = "frozen_test"
        cases.append(case)
    calibration = deepcopy(cases[0])
    calibration["case_id"] = "enterprise-calibration-001"
    calibration["dedup_key"] = "enterprise-calibration-family-001"
    calibration["question"] = "授权企业校准问题？"
    calibration["split"] = "calibration"
    cases.append(calibration)
    dataset = {
        "schema_version": phase10_rag_eval.SCHEMA_VERSION,
        "dataset_id": "authorized-enterprise-test",
        "dataset_kind": "authorized_enterprise",
        "corpus_version": "authorized-corpus-v1",
        "corpus_sha256": "a" * 64,
        "dataset_sha256": phase10_rag_eval.canonical_cases_sha256(cases),
        "cases": cases,
    }

    report = phase10_rag_eval.validate_dataset(dataset, require_enterprise=True)

    assert not report["valid"]
    assert report["frozen_test_case_count"] == 199
    assert report["frozen_valid_case_count"] == 199
    assert report["enterprise_dataset_gate"] == "blocked"
    assert "fewer_than_200_frozen_cases" in report["errors"]
    assert "enterprise_minimum_200_frozen_required" in report["errors"]


def test_reviewed_result_requires_reviewer_date_and_note(tmp_path):
    result = {
        "case_id": "synthetic-p10-001",
        "status": "completed",
        "human_review": {
            "status": "passed",
            "reviewer_ref": "",
            "reviewed_at": None,
            "citation_support": "pass",
            "note": "",
        },
    }

    path = tmp_path / "results.jsonl"
    path.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
    result_map, errors = phase10_rag_eval._read_results(path)

    assert result_map[result["case_id"]] == result
    assert "result:synthetic-p10-001:reviewer_and_date_required" in errors
