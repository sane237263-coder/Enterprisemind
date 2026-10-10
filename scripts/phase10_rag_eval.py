"""Validate and score the Phase-10 RAG acceptance data contract.

This command works on explicitly supplied, locally controlled files.  It never
collects production questions, calls a model, or treats generated data as an
enterprise acceptance result.  The report contains aggregate counts only; it
does not copy questions, answers, SQL, or source text into evidence.

The input is split into a frozen case manifest and a result JSONL.  Keeping
those files separate makes it possible to freeze gold data before running the
candidate and prevents a candidate response from silently becoming its own
gold answer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "p10-rag-case-v1"
DATASET_KINDS = {"authorized_enterprise", "synthetic_preflight"}
SPLITS = {"calibration", "validation", "frozen_test"}
BEHAVIORS = {"answer", "refuse", "clarify"}
STATUSES = {"completed", "failed", "skipped", "not_executed", "blocked"}
HUMAN_STATUSES = {"pending", "passed", "failed"}
SUPPORT_STATUSES = {"pending", "pass", "fail", "not_required"}
CATEGORIES = {
    "single_fact_rule",
    "cross_document",
    "table_numeric",
    "complex_office_ocr",
    "time_department_scope",
    "no_answer",
    "permission_negative",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_CASE_FIELDS = {
    "case_id",
    "dedup_key",
    "split",
    "category",
    "language",
    "question",
    "principal",
    "expected_behavior",
    "gold_evidence",
    "relevance",
    "must_include",
    "must_not_include",
    "numeric_rules",
    "human_checks",
    "source_manifest_refs",
    "authorization_reference",
}


def _text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(ord(char) < 32 and char not in "\t\n\r" for char in value)
    )


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(_text(item) for item in value)


def _canonical_cases(cases: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        cases,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_cases_sha256(cases: list[dict[str, Any]]) -> str:
    """Return the stable hash used to freeze the case manifest."""

    return hashlib.sha256(_canonical_cases(cases)).hexdigest()


def _normalized_question(question: str) -> str:
    return " ".join(question.casefold().split())


def _empty_review() -> dict[str, Any]:
    return {
        "status": "pending",
        "reviewer_ref": None,
        "reviewed_at": None,
        "citation_support": "pending",
        "note": None,
    }


def validate_dataset(
    dataset: dict[str, Any], *, require_enterprise: bool = False
) -> dict[str, Any]:
    """Validate the frozen manifest and return safe aggregate diagnostics."""

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(dataset, dict):
        return {"valid": False, "errors": ["dataset_not_object"]}

    expected_root = {
        "schema_version",
        "dataset_id",
        "dataset_kind",
        "corpus_version",
        "corpus_sha256",
        "dataset_sha256",
        "cases",
    }
    missing_root = expected_root - set(dataset)
    if missing_root:
        errors.append("missing_root_fields:" + ",".join(sorted(missing_root)))
    if dataset.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported_schema_version")
    if not _text(dataset.get("dataset_id")):
        errors.append("invalid_dataset_id")
    kind = dataset.get("dataset_kind")
    if kind not in DATASET_KINDS:
        errors.append("invalid_dataset_kind")
    if not _text(dataset.get("corpus_version")):
        errors.append("invalid_corpus_version")
    for field in ("corpus_sha256", "dataset_sha256"):
        value = dataset.get(field)
        if value is not None and (
            not isinstance(value, str) or not HEX64.fullmatch(value)
        ):
            errors.append("invalid_" + field)

    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases_empty_or_not_list")
        cases = []
    case_ids: list[str] = []
    dedup_keys: list[str] = []
    question_keys: list[str] = []
    frozen_case_ids: list[str] = []
    frozen_dedup_keys: list[str] = []
    frozen_question_keys: list[str] = []
    frozen_valid_case_ids: list[str] = []
    categories: Counter[str] = Counter()
    splits: Counter[str] = Counter()
    for index, case in enumerate(cases):
        prefix = f"case[{index}]"
        case_errors_before = len(errors)
        if not isinstance(case, dict):
            errors.append(prefix + ":not_object")
            continue
        missing = REQUIRED_CASE_FIELDS - set(case)
        if missing:
            errors.append(prefix + ":missing_fields:" + ",".join(sorted(missing)))
        case_id = case.get("case_id")
        if not _text(case_id):
            errors.append(prefix + ":invalid_case_id")
        else:
            case_ids.append(case_id)
        dedup_key = case.get("dedup_key")
        if not _text(dedup_key):
            errors.append(prefix + ":invalid_dedup_key")
        else:
            dedup_keys.append(dedup_key)
        question = case.get("question")
        if not _text(question):
            errors.append(prefix + ":invalid_question")
        else:
            question_keys.append(_normalized_question(question))
        split = case.get("split")
        if split not in SPLITS:
            errors.append(prefix + ":invalid_split")
        else:
            splits[split] += 1
        category = case.get("category")
        if category not in CATEGORIES:
            errors.append(prefix + ":invalid_category")
        else:
            categories[category] += 1
        if not _text(case.get("language")):
            errors.append(prefix + ":invalid_language")
        if not isinstance(case.get("principal"), dict) or not case["principal"]:
            errors.append(prefix + ":invalid_principal")
        behavior = case.get("expected_behavior")
        if behavior not in BEHAVIORS:
            errors.append(prefix + ":invalid_expected_behavior")
        evidence = case.get("gold_evidence")
        if not isinstance(evidence, list):
            errors.append(prefix + ":invalid_gold_evidence")
            evidence = []
        source_ids: list[str] = []
        for source_index, source in enumerate(evidence):
            if (
                not isinstance(source, dict)
                or not _text(source.get("source_id"))
                or not _text(source.get("locator"))
            ):
                errors.append(prefix + f":invalid_gold_evidence[{source_index}]")
                continue
            source_ids.append(source["source_id"])
        if len(source_ids) != len(set(source_ids)):
            errors.append(prefix + ":duplicate_gold_source")
        relevance = case.get("relevance")
        if not isinstance(relevance, dict) or any(
            not _text(key) or type(value) is not int or value not in (0, 1, 2)
            for key, value in (relevance.items() if isinstance(relevance, dict) else [])
        ):
            errors.append(prefix + ":invalid_relevance")
            relevance = {}
        if behavior == "answer" and not source_ids:
            errors.append(prefix + ":answer_without_gold_evidence")
        if not _string_list(case.get("must_include")):
            errors.append(prefix + ":invalid_must_include")
        if not _string_list(case.get("must_not_include")):
            errors.append(prefix + ":invalid_must_not_include")
        numeric_rules = case.get("numeric_rules")
        if not isinstance(numeric_rules, list) or any(
            not isinstance(rule, dict)
            or not _text(rule.get("rule_id"))
            or not _text(rule.get("expected_text"))
            for rule in (numeric_rules if isinstance(numeric_rules, list) else [])
        ):
            errors.append(prefix + ":invalid_numeric_rules")
        if not _string_list(case.get("human_checks")):
            errors.append(prefix + ":invalid_human_checks")
        if not _string_list(case.get("source_manifest_refs")):
            errors.append(prefix + ":invalid_source_manifest_refs")
        if not _text(case.get("authorization_reference")):
            errors.append(prefix + ":invalid_authorization_reference")
        if split == "frozen_test":
            if _text(case_id):
                frozen_case_ids.append(case_id)
            if _text(dedup_key):
                frozen_dedup_keys.append(dedup_key)
            if _text(question):
                frozen_question_keys.append(_normalized_question(question))
            if (
                len(errors) == case_errors_before
                and _text(case_id)
                and _text(dedup_key)
                and _text(question)
            ):
                frozen_valid_case_ids.append(case_id)

    duplicate_ids = sorted(key for key, count in Counter(case_ids).items() if count > 1)
    duplicate_questions = sorted(
        key for key, count in Counter(question_keys).items() if count > 1
    )
    duplicate_dedup_keys = sorted(
        key for key, count in Counter(dedup_keys).items() if count > 1
    )
    duplicate_frozen_case_ids = sorted(
        key for key, count in Counter(frozen_case_ids).items() if count > 1
    )
    duplicate_frozen_dedup_keys = sorted(
        key for key, count in Counter(frozen_dedup_keys).items() if count > 1
    )
    duplicate_frozen_questions = sorted(
        key for key, count in Counter(frozen_question_keys).items() if count > 1
    )
    if duplicate_ids:
        errors.append("duplicate_case_ids")
    if duplicate_questions:
        errors.append("duplicate_questions")
    if duplicate_dedup_keys:
        errors.append("duplicate_dedup_keys")
    if duplicate_frozen_case_ids:
        errors.append("duplicate_frozen_case_ids")
    if duplicate_frozen_dedup_keys:
        errors.append("duplicate_frozen_dedup_keys")
    if duplicate_frozen_questions:
        errors.append("duplicate_frozen_questions")
    computed_hash = canonical_cases_sha256(cases)
    declared_hash = dataset.get("dataset_sha256")
    if declared_hash is not None and declared_hash != computed_hash:
        errors.append("dataset_sha256_mismatch")

    enterprise_gate = "passed"
    if kind != "authorized_enterprise":
        enterprise_gate = "blocked"
        warnings.append("enterprise_authorized_corpus_required")
    else:
        if not isinstance(dataset.get("corpus_sha256"), str) or not HEX64.fullmatch(
            dataset["corpus_sha256"]
        ):
            errors.append("enterprise_corpus_sha256_required")
        if len(frozen_valid_case_ids) < 200:
            enterprise_gate = "blocked"
            errors.append("fewer_than_200_frozen_cases")
        elif (
            duplicate_ids
            or duplicate_questions
            or duplicate_dedup_keys
            or duplicate_frozen_case_ids
            or duplicate_frozen_dedup_keys
            or duplicate_frozen_questions
            or errors
        ):
            enterprise_gate = "failed"
    if require_enterprise and kind != "authorized_enterprise":
        errors.append("enterprise_dataset_required")
    if require_enterprise and len(frozen_valid_case_ids) < 200:
        errors.append("enterprise_minimum_200_frozen_required")

    result = {
        "valid": not errors,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "schema_version": dataset.get("schema_version"),
        "dataset_id": dataset.get("dataset_id"),
        "dataset_kind": kind,
        "corpus_version": dataset.get("corpus_version"),
        "corpus_sha256": dataset.get("corpus_sha256"),
        "dataset_sha256": computed_hash,
        "declared_dataset_sha256": declared_hash,
        "case_count": len(cases),
        "frozen_test_case_count": len(frozen_case_ids),
        "frozen_valid_case_count": len(frozen_valid_case_ids),
        "frozen_unique_case_count": len(set(frozen_case_ids)),
        "frozen_unique_dedup_key_count": len(set(frozen_dedup_keys)),
        "frozen_unique_question_count": len(set(frozen_question_keys)),
        "unique_case_count": len(set(case_ids)),
        "unique_dedup_key_count": len(set(dedup_keys)),
        "unique_question_count": len(set(question_keys)),
        "category_counts": dict(sorted(categories.items())),
        "split_counts": dict(sorted(splits.items())),
        "enterprise_dataset_gate": enterprise_gate,
    }
    return result


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("JSON root must be an object")
    return value


def _read_results(path: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    results: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"result_line_{line_number}:invalid_json")
            continue
        if not isinstance(item, dict) or not _text(item.get("case_id")):
            errors.append(f"result_line_{line_number}:invalid_case_id")
            continue
        case_id = item["case_id"]
        if case_id in results:
            errors.append("duplicate_result_case_id")
        results[case_id] = item
        if item.get("status") not in STATUSES:
            errors.append(f"result:{case_id}:invalid_status")
        for field in (
            "ranked_source_ids",
            "cited_source_ids",
            "valid_citation_source_ids",
            "unauthorized_source_ids",
        ):
            if field in item and not _string_list(item[field]):
                errors.append(f"result:{case_id}:invalid_{field}")
            elif field in item and len(item[field]) != len(set(item[field])):
                errors.append(f"result:{case_id}:duplicate_{field}")
        review = item.get("human_review")
        if review is not None:
            if (
                not isinstance(review, dict)
                or review.get("status") not in HUMAN_STATUSES
            ):
                errors.append(f"result:{case_id}:invalid_human_review")
            else:
                review_status = review["status"]
                if review_status in {"passed", "failed"}:
                    if (
                        not _text(review.get("reviewer_ref"))
                        or not _text(review.get("reviewed_at"))
                        or not _text(review.get("note"))
                    ):
                        errors.append(f"result:{case_id}:reviewer_and_date_required")
                    else:
                        try:
                            datetime.fromisoformat(
                                review["reviewed_at"].replace("Z", "+00:00")
                            )
                        except ValueError:
                            errors.append(f"result:{case_id}:invalid_reviewed_at")
                if review.get("citation_support", "pending") not in SUPPORT_STATUSES:
                    errors.append(f"result:{case_id}:invalid_citation_support")
        if "numeric_rule_results" in item:
            values = item["numeric_rule_results"]
            if not isinstance(values, dict) or any(
                type(value) is not bool for value in values.values()
            ):
                errors.append(f"result:{case_id}:invalid_numeric_rule_results")
    return results, sorted(set(errors))


def _case_score(case: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    expected = case["expected_behavior"]
    required = {
        source["source_id"]
        for source in case["gold_evidence"]
        if isinstance(source, dict)
    }
    relevance = case["relevance"]
    positive = {source_id for source_id, grade in relevance.items() if grade > 0}
    if result is None:
        result = {"status": "not_executed"}
    status = result.get("status")
    completed = status == "completed"
    ranked = result.get("ranked_source_ids", [])
    cited = set(result.get("cited_source_ids", []))
    valid_citations = set(result.get("valid_citation_source_ids", []))
    unauthorized = set(result.get("unauthorized_source_ids", []))
    top10 = set(ranked[:10])
    answer_text = (
        result.get("answer_text", "")
        if isinstance(result.get("answer_text"), str)
        else ""
    )
    answer_folded = answer_text.casefold()
    must_include = all(
        value.casefold() in answer_folded for value in case["must_include"]
    )
    must_not_include = not any(
        value.casefold() in answer_folded for value in case["must_not_include"]
    )
    behavior_correct = completed and result.get("observed_behavior") == expected
    citation_required = expected == "answer" and bool(required)
    citation_parseable = (
        completed
        and (not citation_required or bool(cited))
        and cited == valid_citations
        and result.get("citation_locators_valid", True) is True
    )
    citation_authorized = (
        completed
        and not unauthorized
        and valid_citations.issubset(
            set(result.get("authorized_source_ids", valid_citations))
        )
    )
    evidence_recall = (
        len(top10 & required) / len(required) if required and completed else 0.0
    )
    hit = bool(top10 & positive) if completed else False
    numeric_results = result.get("numeric_rule_results", {})
    numeric_complete = all(
        rule["rule_id"] in numeric_results for rule in case["numeric_rules"]
    )
    numeric_passed = numeric_complete and all(
        numeric_results.get(rule["rule_id"]) is True for rule in case["numeric_rules"]
    )
    auto_answer_ok = (
        behavior_correct
        and (must_include if expected == "answer" or case["must_include"] else True)
        and must_not_include
        and (numeric_passed if case["numeric_rules"] else True)
    )
    review = result.get("human_review") or _empty_review()
    review_status = (
        review.get("status", "pending") if isinstance(review, dict) else "pending"
    )
    support = (
        review.get("citation_support", "pending")
        if isinstance(review, dict)
        else "pending"
    )
    return {
        "status": status,
        "completed": completed,
        "hit_at_10": hit,
        "required_evidence_recall": evidence_recall,
        "citation_required": citation_required,
        "citation_parseable": citation_parseable,
        "citation_authorized": citation_authorized,
        "permission_violation_count": len(unauthorized),
        "behavior_correct": behavior_correct,
        "answer_rules_passed": must_include and must_not_include and numeric_passed
        if expected == "answer"
        else must_include and must_not_include,
        "numeric_rules_complete": numeric_complete,
        "auto_answer_ok": auto_answer_ok,
        "human_review_status": review_status,
        "citation_support": support,
    }


def score_dataset(
    dataset: dict[str, Any],
    results: dict[str, dict[str, Any]],
    result_errors: list[str],
) -> dict[str, Any]:
    all_cases = dataset.get("cases", [])
    cases = [
        case
        for case in all_cases
        if isinstance(case, dict) and case.get("split") == "frozen_test"
    ]
    case_ids = {case.get("case_id") for case in all_cases if isinstance(case, dict)}
    frozen_case_ids = {case.get("case_id") for case in cases}
    unknown_results = sorted(set(results) - case_ids)
    errors = sorted(
        set(result_errors + (["unknown_result_case_id"] if unknown_results else []))
    )
    non_frozen_results = sorted(set(results) & (case_ids - frozen_case_ids))
    scored = [
        _case_score(case, results.get(case.get("case_id")))
        for case in cases
        if isinstance(case, dict) and _text(case.get("case_id"))
    ]
    total = len(scored)
    completed = sum(item["completed"] for item in scored)
    answerable = [
        item
        for case, item in zip(cases, scored, strict=False)
        if case["expected_behavior"] == "answer"
    ]
    no_answer = [
        item
        for case, item in zip(cases, scored, strict=False)
        if case["expected_behavior"] in {"refuse", "clarify"}
    ]
    citation_cases = [item for item in answerable if item["citation_required"]]
    human_required = [item for item in scored]
    reviewed = [
        item
        for item in human_required
        if item["human_review_status"] in {"passed", "failed"}
    ]
    support_reviewed = [
        item
        for item in human_required
        if item["citation_required"] and item["citation_support"] in {"pass", "fail"}
    ]
    support_passed = sum(
        item["citation_support"] == "pass" for item in support_reviewed
    )
    permission_violations = sum(item["permission_violation_count"] for item in scored)
    validation = validate_dataset(dataset)
    hit_count = sum(item["hit_at_10"] for item in answerable)
    correct_count = sum(item["auto_answer_ok"] for item in answerable)
    no_answer_correct = sum(item["behavior_correct"] for item in no_answer)
    parseable_count = sum(item["citation_parseable"] for item in citation_cases)
    authorized_count = sum(item["citation_authorized"] for item in citation_cases)
    auto_gate = (
        not errors
        and permission_violations == 0
        and total > 0
        and completed == total
        and (not answerable or hit_count / len(answerable) >= 0.90)
        and (not citation_cases or parseable_count / len(citation_cases) == 1.0)
        and (not citation_cases or authorized_count / len(citation_cases) == 1.0)
        and (not no_answer or no_answer_correct / len(no_answer) >= 0.95)
    )
    human_gate = (
        not errors
        and len(reviewed) == len(human_required)
        and len(support_reviewed) == len(citation_cases)
        and (not support_reviewed or support_passed / len(support_reviewed) >= 0.95)
        and (
            not answerable
            or sum(item["human_review_status"] == "passed" for item in answerable)
            / len(answerable)
            >= 0.85
        )
    )
    dataset_gate = (
        validation["enterprise_dataset_gate"] == "passed" and validation["valid"]
    )
    overall = "passed" if dataset_gate and auto_gate and human_gate else "blocked"
    if errors or validation["errors"]:
        overall = "failed" if validation["errors"] and dataset_gate else "blocked"
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": {
            "dataset_id": dataset.get("dataset_id"),
            "dataset_kind": dataset.get("dataset_kind"),
            "corpus_version": dataset.get("corpus_version"),
            "corpus_sha256": dataset.get("corpus_sha256"),
            "dataset_sha256": validation["dataset_sha256"],
            "case_count": total,
            "calibration_case_count": sum(
                isinstance(case, dict) and case.get("split") == "calibration"
                for case in all_cases
            ),
            "validation_case_count": sum(
                isinstance(case, dict) and case.get("split") == "validation"
                for case in all_cases
            ),
            "frozen_test_case_count": total,
            "enterprise_dataset_gate": validation["enterprise_dataset_gate"],
        },
        "execution": {
            "completed": completed,
            "failed": sum(item["status"] == "failed" for item in scored),
            "skipped": sum(item["status"] == "skipped" for item in scored),
            "not_executed": sum(item["status"] == "not_executed" for item in scored),
            "blocked": sum(item["status"] == "blocked" for item in scored),
            "completion_denominator": total,
            "completion_rate": completed / total if total else 0.0,
        },
        "automatic": {
            "answerable_denominator": len(answerable),
            "hit_at_10": hit_count / len(answerable) if answerable else None,
            "required_evidence_recall_at_10": sum(
                item["required_evidence_recall"] for item in answerable
            )
            / len(answerable)
            if answerable
            else None,
            "answerable_auto_quality": correct_count / len(answerable)
            if answerable
            else None,
            "no_answer_denominator": len(no_answer),
            "no_answer_correctness": no_answer_correct / len(no_answer)
            if no_answer
            else None,
            "citation_cases": len(citation_cases),
            "citation_parseable_rate": parseable_count / len(citation_cases)
            if citation_cases
            else None,
            "citation_authorized_rate": authorized_count / len(citation_cases)
            if citation_cases
            else None,
            "permission_violations": permission_violations,
            "result_errors": errors,
            "non_frozen_results_ignored": len(non_frozen_results),
        },
        "human_review": {
            "required_denominator": len(human_required),
            "reviewed": len(reviewed),
            "pending_or_missing": len(human_required) - len(reviewed),
            "citation_support_denominator": len(support_reviewed),
            "citation_support_pending": len(citation_cases) - len(support_reviewed),
            "citation_support_rate": support_passed / len(support_reviewed)
            if support_reviewed
            else None,
        },
        "gates": {
            "dataset": "passed" if dataset_gate else "blocked",
            "automatic": "passed" if auto_gate else "blocked",
            "human": "passed" if human_gate else "blocked",
            "overall": overall,
        },
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def build_template() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": "SET_DATASET_ID",
        "dataset_kind": "authorized_enterprise",
        "corpus_version": "SET_CONTROLLED_CORPUS_VERSION",
        "corpus_sha256": None,
        "dataset_sha256": None,
        "cases": [
            {
                "case_id": "p10-case-001",
                "dedup_key": "SET_UNIQUE_SEMANTIC_CASE_KEY",
                "split": "frozen_test",
                "category": "single_fact_rule",
                "language": "zh-CN",
                "question": "填写授权企业问题，不复制被测答案。",
                "principal": {
                    "user_ref": "controlled-user-ref",
                    "department_ref": "controlled-department-ref",
                    "allowed_scope_ref": "controlled-scope-ref",
                },
                "expected_behavior": "answer",
                "gold_evidence": [
                    {"source_id": "doc-ref-001", "locator": "page:1/paragraph:1"}
                ],
                "relevance": {"doc-ref-001": 2},
                "must_include": ["填写必要事实或数值"],
                "must_not_include": ["禁止出现的越权或编造内容"],
                "numeric_rules": [],
                "human_checks": ["citation_support", "conditions_complete"],
                "source_manifest_refs": ["controlled-corpus-manifest-ref"],
                "authorization_reference": "approval-or-redaction-ref",
            }
        ],
    }


def build_synthetic() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cases: list[dict[str, Any]] = []

    def add(
        number: int,
        category: str,
        behavior: str,
        question: str,
        evidence: list[dict[str, str]],
        include: list[str],
        source_ref: str = "synthetic-manifest-v1",
    ) -> None:
        source_ids = [item["source_id"] for item in evidence]
        cases.append(
            {
                "case_id": f"synthetic-p10-{number:03d}",
                "dedup_key": f"synthetic-family-{number:03d}",
                "split": "frozen_test" if number > 4 else "calibration",
                "category": category,
                "language": "zh-CN",
                "question": question,
                "principal": {
                    "user_ref": "synthetic-user",
                    "department_ref": "synthetic-department",
                    "allowed_scope_ref": "synthetic-scope",
                },
                "expected_behavior": behavior,
                "gold_evidence": evidence,
                "relevance": {source_id: 2 for source_id in source_ids},
                "must_include": include,
                "must_not_include": ["SYNTHETIC_UNAUTHORIZED_SENTINEL"],
                "numeric_rules": [],
                "human_checks": ["citation_support", "conditions_complete"],
                "source_manifest_refs": [source_ref],
                "authorization_reference": "synthetic-approval-do-not-use-as-enterprise",
            }
        )

    add(
        1,
        "single_fact_rule",
        "answer",
        "合成资料的报销期限是多少？",
        [{"source_id": "syn-doc-1", "locator": "p:1"}],
        ["30天"],
    )
    add(
        2,
        "table_numeric",
        "answer",
        "合成表格中的额度是多少？",
        [{"source_id": "syn-doc-2", "locator": "sheet:Data!A1"}],
        ["100"],
    )
    add(
        3,
        "cross_document",
        "answer",
        "两份合成资料的共同条件是什么？",
        [
            {"source_id": "syn-doc-3", "locator": "p:2"},
            {"source_id": "syn-doc-4", "locator": "p:4"},
        ],
        ["共同条件"],
    )
    add(
        4,
        "no_answer",
        "refuse",
        "合成资料没有覆盖的事实是什么？",
        [],
        ["没有找到足够依据"],
    )
    add(
        5,
        "permission_negative",
        "refuse",
        "无权资料中隐藏的内容是什么？",
        [],
        ["没有找到足够依据"],
    )
    add(
        6,
        "time_department_scope",
        "clarify",
        "请比较两个未说明范围的版本。",
        [{"source_id": "syn-doc-6", "locator": "p:1"}],
        ["请提供范围"],
    )
    add(
        7,
        "complex_office_ocr",
        "answer",
        "扫描合成表的状态是什么？",
        [{"source_id": "syn-doc-7", "locator": "page:1/table:1"}],
        ["已批准"],
    )
    add(
        8,
        "single_fact_rule",
        "answer",
        "第二个合成事实是什么？",
        [{"source_id": "syn-doc-8", "locator": "p:3"}],
        ["事实二"],
    )
    dataset = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": "phase10-synthetic-preflight-v1",
        "dataset_kind": "synthetic_preflight",
        "corpus_version": "synthetic-corpus-v1",
        "corpus_sha256": "0" * 64,
        "dataset_sha256": canonical_cases_sha256(cases),
        "cases": cases,
    }
    dataset["corpus_sha256"] = canonical_cases_sha256(cases)
    results = [
        {
            "case_id": "synthetic-p10-001",
            "status": "completed",
            "observed_behavior": "answer",
            "ranked_source_ids": ["syn-doc-1"],
            "cited_source_ids": ["syn-doc-1"],
            "valid_citation_source_ids": ["syn-doc-1"],
            "authorized_source_ids": ["syn-doc-1"],
            "answer_text": "合成资料显示期限为30天。",
            "human_review": _empty_review(),
        },
        {
            "case_id": "synthetic-p10-002",
            "status": "completed",
            "observed_behavior": "answer",
            "ranked_source_ids": ["syn-doc-2"],
            "cited_source_ids": ["syn-doc-2"],
            "valid_citation_source_ids": [],
            "authorized_source_ids": ["syn-doc-2"],
            "answer_text": "额度为100。",
            "human_review": _empty_review(),
        },
        {
            "case_id": "synthetic-p10-004",
            "status": "completed",
            "observed_behavior": "refuse",
            "ranked_source_ids": [],
            "cited_source_ids": [],
            "valid_citation_source_ids": [],
            "authorized_source_ids": [],
            "answer_text": "没有找到足够依据。",
            "human_review": _empty_review(),
        },
        {
            "case_id": "synthetic-p10-005",
            "status": "completed",
            "observed_behavior": "refuse",
            "ranked_source_ids": [],
            "cited_source_ids": [],
            "valid_citation_source_ids": [],
            "authorized_source_ids": [],
            "unauthorized_source_ids": [],
            "answer_text": "没有找到足够依据。",
            "human_review": _empty_review(),
        },
        {
            "case_id": "synthetic-p10-006",
            "status": "completed",
            "observed_behavior": "clarify",
            "ranked_source_ids": [],
            "cited_source_ids": [],
            "valid_citation_source_ids": [],
            "authorized_source_ids": [],
            "answer_text": "请提供范围。",
            "human_review": _empty_review(),
        },
        {
            "case_id": "synthetic-p10-007",
            "status": "failed",
            "execution_error_code": "MODEL_PROVIDER_UNAVAILABLE",
            "human_review": _empty_review(),
        },
        {
            "case_id": "synthetic-p10-008",
            "status": "blocked",
            "execution_error_code": "NO_AUTHORIZED_CORPUS",
            "human_review": _empty_review(),
        },
    ]
    return dataset, results


def _write_results(path: Path, results: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
            for item in results
        ),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    template = sub.add_parser("make-template")
    template.add_argument("--output", type=Path, required=True)
    synthetic = sub.add_parser("make-synthetic")
    synthetic.add_argument("--dataset", type=Path, required=True)
    synthetic.add_argument("--results", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--dataset", type=Path, required=True)
    validate.add_argument("--output", type=Path)
    validate.add_argument("--require-enterprise", action="store_true")
    score = sub.add_parser("score")
    score.add_argument("--dataset", type=Path, required=True)
    score.add_argument("--results", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    score.add_argument("--enforce-final", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "make-template":
            _write_json(args.output, build_template())
            print(json.dumps({"status": "written", "path": str(args.output.resolve())}))
            return 0
        if args.command == "make-synthetic":
            dataset, results = build_synthetic()
            _write_json(args.dataset, dataset)
            _write_results(args.results, results)
            print(
                json.dumps(
                    {
                        "status": "written",
                        "dataset": str(args.dataset.resolve()),
                        "results": str(args.results.resolve()),
                    }
                )
            )
            return 0
        dataset = _read_json(args.dataset)
        if args.command == "validate":
            report = validate_dataset(
                dataset, require_enterprise=args.require_enterprise
            )
            if args.output:
                _write_json(args.output, report)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return (
                0
                if report["valid"]
                and (
                    not args.require_enterprise
                    or report["enterprise_dataset_gate"] == "passed"
                )
                else 2
            )
        results, result_errors = _read_results(args.results)
        report = score_dataset(dataset, results, result_errors)
        _write_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return (
            0 if not args.enforce_final or report["gates"]["overall"] == "passed" else 2
        )
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        print(
            f"phase10-rag-eval error: {type(error).__name__}: no report written",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
