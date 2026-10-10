"""Render the bounded Phase 10 chart fixture into a local evidence directory.

This is a deterministic synthetic preflight.  It exercises the same chart
contracts used by the future API/worker path and records the actual artifact
hashes, but it is deliberately marked as synthetic and cannot satisfy the
enterprise authorization or external integration gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / "backend" / "src"))

DATA_VERSION = "phase10-chart-synthetic-v1"
QUERIED_AT = "2026-09-22T10:00:00+08:00"
SOURCE_REF = f"synthetic:{DATA_VERSION}"


def _charts():
    from enterprise_mind import charts

    return charts


def _spec(chart_type: str):
    line = chart_type == "line"
    return _charts().ChartSpec(
        chart_type=chart_type,
        title="企业部门指标图",
        data_ref=f"preflight:{DATA_VERSION}:{chart_type}",
        dimension_column="month" if line else "department",
        metric_column="amount",
        statistic="SUM(amount) 按月聚合" if line else "SUM(amount) 按部门聚合",
        scope="all_aggregated",
        scope_label=(
            "全部匹配时间点（已按月份升序）" if line else "全部匹配数据（按部门聚合）"
        ),
        source_refs=(SOURCE_REF,),
        queried_at=QUERIED_AT,
        revision=f"{DATA_VERSION}:{chart_type}",
        unit="元",
        sort_order="ascending" if line else "input",
        dimension_kind="time" if line else "category",
        aggregate_complete=True,
    )


def _snapshot(chart_type: str):
    charts = _charts()
    if chart_type == "line":
        rows = [
            charts.ChartPoint("2026-03-01", Decimal("30.00")),
            charts.ChartPoint("2026-01-01", Decimal("10.00")),
            charts.ChartPoint("2026-02-01", Decimal("20.00")),
        ]
    else:
        rows = [
            charts.ChartPoint("华东", Decimal("10.00")),
            charts.ChartPoint("华南", Decimal("20.00")),
            charts.ChartPoint("华北", Decimal("30.00")),
        ]
    return charts.ChartSnapshot.freeze(_spec(chart_type), rows)


def _fixture_digest() -> str:
    payload = {
        "data_version": DATA_VERSION,
        "queried_at": QUERIED_AT,
        "source_ref": SOURCE_REF,
        "rows": {
            "bar": [["华东", "10.00"], ["华南", "20.00"], ["华北", "30.00"]],
            "line": [
                ["2026-03-01", "30.00"],
                ["2026-01-01", "10.00"],
                ["2026-02-01", "20.00"],
            ],
            "pie": [["华东", "10.00"], ["华南", "20.00"], ["华北", "30.00"]],
        },
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _safe_output_dir(value: str) -> Path:
    candidate = Path(value)
    target = (
        WORKSPACE / candidate if not candidate.is_absolute() else candidate
    ).resolve()
    evidence_root = (WORKSPACE / ".local" / "phase10").resolve()
    try:
        target.relative_to(evidence_root)
    except ValueError as error:
        raise SystemExit(f"output-dir must remain under {evidence_root}") from error
    target.mkdir(parents=True, exist_ok=True)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", required=True, help="local .local/phase10 evidence directory"
    )
    args = parser.parse_args()
    output_dir = _safe_output_dir(args.output_dir)
    artifacts: list[dict[str, object]] = []
    chart_types = ("bar", "line", "pie")
    formats = ("png", "pdf", "xlsx", "csv")
    for chart_type in chart_types:
        snapshot = _snapshot(chart_type)
        rendered = _charts().render_chart(snapshot, formats, output_dir)
        for chart_format in formats:
            artifact = rendered[chart_format]
            artifacts.append(
                {
                    "chart_type": chart_type,
                    "format": chart_format,
                    "path": str(artifact.path),
                    "mime_type": artifact.mime_type,
                    "size_bytes": artifact.size,
                    "sha256": artifact.sha256,
                    "snapshot_hash": snapshot.spec.content_hash,
                }
            )
    manifest = {
        "schema_version": "phase10.chart-preflight/v1",
        "candidate": "working-tree",
        "synthetic_only": True,
        "enterprise_gate": "blocked_without_authorized_corpus_and_reviewer",
        "data_version": DATA_VERSION,
        "data_sha256": _fixture_digest(),
        "queried_at": QUERIED_AT,
        "chart_types": list(chart_types),
        "formats": list(formats),
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"manifest": str(manifest_path), "artifact_count": len(artifacts)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
