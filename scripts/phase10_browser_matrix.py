"""Create and maintain the Phase 10 browser acceptance matrix.

This helper deliberately has no browser automation dependency.  Playwright is
kept in the frontend as an optional, pinned acceptance dependency; this helper
owns the repeatable evidence boundary around it: an isolated run directory, a
source manifest, a runtime preflight, and explicit result records that
distinguish real application paths from deterministic fault paths.

Examples (repository root)::

    python scripts/phase10_browser_matrix.py \
      --run-id 20260922-p10-07-browser-u01 \
      --base-url http://127.0.0.1:5173 \
      --runtime-dir .local/phase10/20260921-phase10-baseline-lunamax-01/runtime \
      preflight

    python scripts/phase10_browser_matrix.py \
      --run-id 20260922-p10-07-browser-u01 \
      record P10-07-AUTH-LOGIN passed --browser chromium \
      --layer real --note "login and logout clicked in a clean context" \
      --artifact browser/screenshots/login.png

The command never stores usernames, passwords, bearer tokens, cookies, or
response bodies.  Browser traces and downloads should be placed below the
run's ``browser/`` directory by the browser tool and referenced with
``--artifact``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = ROOT / ".local" / "phase10"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,127}$")
SAFE_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9_./\\-]+$")


@dataclass(frozen=True)
class Case:
    case_id: str
    area: str
    title: str
    browser_scope: str
    layer: str
    requires: tuple[str, ...] = ()
    negative: bool = False
    expected_observations: tuple[str, ...] = ()


CASES: tuple[Case, ...] = (
    Case(
        "P10-07-HARNESS-BROWSER-START",
        "harness",
        "browser launch and static frontend shell",
        "chromium+edge",
        "deterministic",
        ("Vite frontend",),
        expected_observations=("browser process launches", "frontend root returns 2xx", "login shell is visible"),
    ),
    Case(
        "P10-07-AUTH-LOGIN",
        "identity",
        "valid login, session identity, logout",
        "chromium+edge",
        "real",
        ("API",),
        expected_observations=("username/role visible", "logout returns to login"),
    ),
    Case(
        "P10-07-EDGE-SMOKE",
        "identity",
        "Windows Edge representative login, source-scope shell and logout",
        "edge",
        "real",
        ("API", "Windows Edge"),
        expected_observations=("Edge launches", "source-scope note is visible", "logout returns to login"),
    ),
    Case(
        "P10-07-AUTH-INVALID",
        "identity",
        "invalid credentials show server error",
        "chromium",
        "real",
        ("API",),
        negative=True,
        expected_observations=("no authenticated workspace", "error remains user-readable"),
    ),
    Case(
        "P10-07-AUTH-EXPIRY-DISABLED",
        "identity",
        "expired or disabled identity is rejected",
        "chromium",
        "real",
        ("API", "identity fixture"),
        negative=True,
        expected_observations=("session-expired returns to login", "no stale page access"),
    ),
    Case(
        "P10-07-AUTH-DEPARTMENT-REVOKE",
        "identity",
        "department change and queued access revocation",
        "chromium",
        "real",
        ("API", "two isolated identities"),
        negative=True,
        expected_observations=("department change is visible", "revoked source/file/query is denied after change"),
    ),
    Case(
        "P10-07-NAV-ALL-PAGES",
        "identity",
        "authenticated navigation across agent, knowledge, memory, recycle and admin pages",
        "chromium+edge",
        "real",
        ("API",),
        expected_observations=("each permitted page is visible", "admin page is hidden for ordinary user"),
    ),
    Case(
        "P10-07-ADMIN-AUTHZ",
        "management",
        "administrator management and ordinary-user denial",
        "chromium",
        "real",
        ("API", "two isolated identities"),
        negative=True,
        expected_observations=("admin route visible only for admin", "forbidden action has no side effect"),
    ),
    Case(
        "P10-07-ADMIN-MANAGEMENT",
        "management",
        "administrator creates an isolated department and ordinary user",
        "chromium",
        "real",
        ("API", "admin identity", "Secret file"),
        expected_observations=("department appears in the management table", "ordinary user is bound to that department"),
    ),
    Case(
        "P10-07-ORDINARY-AUTHZ",
        "authorization",
        "ordinary user cannot see administrator navigation and can use permitted pages",
        "chromium",
        "real",
        ("API", "ordinary identity"),
        negative=True,
        expected_observations=("member management is absent", "knowledge/memory/recycle pages remain permitted"),
    ),
    Case(
        "P10-07-KB-PUBLIC-DEPARTMENT",
        "management",
        "public and department knowledge bases with ordinary-user authorization",
        "chromium",
        "real",
        ("API", "two isolated identities"),
        negative=True,
        expected_observations=("public item is readable by policy", "department item is hidden outside department"),
    ),
    Case(
        "P10-07-KNOWLEDGE-LIFECYCLE",
        "documents",
        "folders, upload, parse progress/failure, download",
        "chromium",
        "real",
        ("API", "Worker", "MinIO"),
        expected_observations=("actual object/file state", "visible parse outcome"),
    ),
    Case(
        "P10-07-DOC-UPLOAD-SAME-NAME",
        "documents",
        "upload into an existing folder and same-name disambiguation",
        "chromium",
        "real",
        ("API", "MinIO"),
        expected_observations=("both names are unambiguous", "selected destination is visible"),
    ),
    Case(
        "P10-07-DOC-PARSE-FAILURE-RETRY",
        "documents",
        "parse failure and bounded retry state",
        "chromium",
        "deterministic",
        ("API", "Worker", "controlled fault"),
        negative=True,
        expected_observations=("failure reason is visible", "retry does not create duplicate document"),
    ),
    Case(
        "P10-07-RECYCLE-AUTHZ",
        "documents",
        "recycle-bin delete/restore and permission boundary",
        "chromium",
        "real",
        ("API", "Worker"),
        negative=True,
        expected_observations=("restore changes visible list", "other user cannot restore"),
    ),
    Case(
        "P10-07-RAG-AGENT",
        "rag-agent",
        "source-backed answer, no-answer, stream, stop/resume, confirmation",
        "chromium+edge",
        "real",
        ("API", "Worker", "current model"),
        expected_observations=("visible source cards", "stream reaches terminal state", "confirmation remains explicit"),
    ),
    Case(
        "P10-07-RAG-NO-ANSWER-SCOPE",
        "rag-agent",
        "no-answer, department scope and time-range filtering",
        "chromium",
        "real",
        ("API", "Worker", "current model"),
        negative=True,
        expected_observations=("no-answer is explicit", "out-of-scope source is absent"),
    ),
    Case(
        "P10-07-RAG-SOURCE-SESSION-STOP",
        "rag-agent",
        "source preview, session isolation, stop and continue",
        "chromium+edge",
        "real",
        ("API", "Worker", "current model"),
        expected_observations=("source locator opens", "stopping and continuing preserve terminal facts"),
    ),
    Case(
        "P10-07-OFFICE-VISUAL",
        "office-visual",
        "DOCX/XLSX/PDF/image preview, OCR/Vision and partial failure message",
        "chromium",
        "real",
        ("API", "Worker", "MinIO", "OCR/Vision provider"),
        expected_observations=("preview bytes render", "position/source information visible"),
    ),
    Case(
        "P10-07-OFFICE-OCR",
        "office-visual",
        "OCR result and unsupported/partial explanation",
        "chromium",
        "real",
        ("API", "Worker", "MinIO", "OCR provider"),
        expected_observations=("recognized text has locator", "partial state is not reported as success"),
    ),
    Case(
        "P10-07-OFFICE-VISION",
        "office-visual",
        "Vision result and image source locator",
        "chromium",
        "real",
        ("API", "Worker", "MinIO", "Vision provider"),
        expected_observations=("image semantics are labeled", "source permission still applies"),
    ),
    Case(
        "P10-07-OFFICE-FAILURE-DETAIL",
        "office-visual",
        "failed image processing reason and retry guidance",
        "chromium",
        "real",
        ("API", "Worker", "MinIO", "OCR/Vision provider"),
        negative=True,
        expected_observations=("failure code is visible", "retry guidance is visible", "no image success is claimed"),
    ),
    Case(
        "P10-07-DATABASE-QUERY",
        "database",
        "authorized aggregate/detail/continuation and source",
        "chromium+edge",
        "real",
        ("API", "Worker", "independent MCP", "business PostgreSQL"),
        expected_observations=("source relation/row count visible", "continuation preserves row semantics"),
    ),
    Case(
        "P10-07-DATABASE-DETAIL-CONTINUATION",
        "database",
        "database detail rows, continuation page and duplicate-row preservation",
        "chromium+edge",
        "real",
        ("API", "Worker", "independent MCP", "business PostgreSQL"),
        expected_observations=("current-page count is visible", "next page does not overwrite prior page"),
    ),
    Case(
        "P10-07-DATABASE-DENY",
        "database",
        "database whitelist/department denial and unsafe-query failure",
        "chromium",
        "real",
        ("API", "independent MCP", "two isolated identities"),
        negative=True,
        expected_observations=("rejected query has no download", "error does not reveal SQL/secrets"),
    ),
    Case(
        "P10-07-DATABASE-EXPORT-FAILURE",
        "database",
        "database export budget/owner restart/column-frame failure",
        "chromium",
        "deterministic",
        ("API", "Worker", "independent MCP", "controlled fault"),
        negative=True,
        expected_observations=("stale plan asks for re-query", "no partial workbook is downloadable"),
    ),
    Case(
        "P10-07-MEMORY",
        "memory",
        "own Memory read/create/update/delete and new-session effect",
        "chromium",
        "real",
        ("API", "two isolated identities"),
        expected_observations=("own entries visible", "other user has no detail/list/context leakage"),
    ),
    Case(
        "P10-07-MEMORY-OTHER-USER",
        "memory",
        "other-user Memory list/detail/model-context denial",
        "chromium",
        "real",
        ("API", "two isolated identities"),
        negative=True,
        expected_observations=("no cross-user list/detail", "no cross-user context leakage"),
    ),
    Case(
        "P10-07-CONTENT-FOUR-FORMATS",
        "content-export",
        "frozen content draft to Markdown/DOCX/PDF/XLSX downloads",
        "chromium",
        "real",
        ("API", "Worker", "MinIO"),
        expected_observations=("four actual byte streams", "hash/size/MIME recorded", "source visible"),
    ),
    Case(
        "P10-07-CONTENT-DRAFT-FREEZE",
        "content-export",
        "draft edit after queueing does not change frozen four-format input",
        "chromium",
        "real",
        ("API", "Worker", "MinIO"),
        expected_observations=("D1 and D2 produce distinct task facts", "download hash matches frozen D1"),
    ),
    Case(
        "P10-07-PREVIEW-CONFIRM",
        "preview-confirm",
        "unconfirmed preview, target binding, idempotent confirmation, searchable result",
        "chromium",
        "real",
        ("API", "Worker", "MinIO", "parser/indexer"),
        negative=True,
        expected_observations=("no document before confirmation", "one document after duplicate confirmation", "index is reachable"),
    ),
    Case(
        "P10-07-DOCUMENT-PREVIEW",
        "office-visual",
        "authenticated document preview loads actual stored Office/PDF bytes",
        "chromium",
        "real",
        ("API", "Worker", "MinIO"),
        expected_observations=("preview modal loads", "original download action is visible"),
    ),
    Case(
        "P10-07-PREVIEW-REPEAT-TARGET",
        "preview-confirm",
        "repeat confirmation and changed target are rejected or idempotent",
        "chromium",
        "real",
        ("API", "Worker", "MinIO", "parser/indexer"),
        negative=True,
        expected_observations=("duplicate confirmation creates one document", "changed target requires a fresh confirmation"),
    ),
    Case(
        "P10-07-CHART-BAR-FOUR-FORMATS",
        "charts",
        "bar chart preview and PNG/PDF/XLSX/CSV exports",
        "chromium",
        "real",
        ("API", "Worker", "MinIO", "authorized database aggregate"),
        expected_observations=("four real artifacts", "XLSX native chart/data/source sheets", "CSV metadata and byte hash"),
    ),
    Case(
        "P10-07-CHART-LINE-FOUR-FORMATS",
        "charts",
        "line chart preview and PNG/PDF/XLSX/CSV exports",
        "chromium",
        "real",
        ("API", "Worker", "MinIO", "authorized database aggregate"),
        expected_observations=("four real artifacts", "time ordering and gap semantics are visible", "CSV metadata and byte hash"),
    ),
    Case(
        "P10-07-CHART-PIE-FOUR-FORMATS",
        "charts",
        "pie chart preview and PNG/PDF/XLSX/CSV exports",
        "chromium",
        "real",
        ("API", "Worker", "MinIO", "authorized database aggregate"),
        expected_observations=("four real artifacts", "negative/all-zero input is rejected", "CSV metadata and byte hash"),
    ),
    Case(
        "P10-07-FAILURE-CANCEL",
        "failure-cancel",
        "render/upload/provider failure, cancel, browser refresh and recovery",
        "chromium",
        "deterministic",
        ("API", "Worker", "controlled fault"),
        negative=True,
        expected_observations=("failed/cancelled terminal state", "no false download", "refresh does not silently cancel"),
    ),
    Case(
        "P10-07-UNAUTHORIZED-OBJECTS",
        "authorization",
        "cross-user/dept object, file, preview and download denial",
        "chromium",
        "real",
        ("API", "two isolated identities", "MinIO"),
        negative=True,
        expected_observations=("403/404 as designed", "no object bytes or metadata leakage"),
    ),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_dir(run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise SystemExit(f"invalid run id: {run_id!r}")
    return EVIDENCE_ROOT / run_id / "browser"


def json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def manifest_paths() -> Iterable[Path]:
    roots = (
        ROOT / "backend" / "src",
        ROOT / "backend" / "tests",
        ROOT / "backend" / "migrations",
        ROOT / "frontend" / "src",
        ROOT / "frontend" / "tests",
        ROOT / "scripts",
        ROOT / "config",
        ROOT / "deploy",
    )
    files: set[Path] = set()
    for directory in roots:
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            rel = safe_rel(path)
            if any(part in {".venv", "node_modules", "__pycache__", ".local"} for part in path.parts):
                continue
            lowered = rel.lower()
            if any(token in lowered for token in (".env", "secret", "token", "api_key", "password")):
                continue
            files.add(path)
    for path in (
        ROOT / "backend" / "pyproject.toml",
        ROOT / "backend" / "uv.lock",
        ROOT / "frontend" / "package.json",
        ROOT / "frontend" / "package-lock.json",
        ROOT / "compose.test.yaml",
    ):
        if path.is_file():
            files.add(path)
    return sorted(files, key=safe_rel)


def write_source_manifest(directory: Path) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    records: list[dict[str, Any]] = []
    tsv: list[str] = []
    for path in manifest_paths():
        data = path.read_bytes()
        file_hash = hashlib.sha256(data).hexdigest()
        rel = safe_rel(path)
        record = {"path": rel, "size": len(data), "sha256": file_hash}
        records.append(record)
        tsv.append(f"{rel}\t{len(data)}\t{file_hash}")
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    (directory / "source-manifest.tsv").write_text("path\tsize\tsha256\n" + "\n".join(tsv) + "\n", encoding="utf-8")
    return {"sha256": digest.hexdigest(), "file_count": len(records), "files": records}


def git_identity() -> dict[str, Any]:
    def git(*args: str) -> str | None:
        try:
            return subprocess.check_output(["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    status = git("status", "--short", "--untracked-files=no")
    return {"head": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"), "tracked_status": status or "clean"}


def _runtime_file(runtime_dir: Path | None, runtime_file: Path | None) -> Path | None:
    if runtime_file is not None:
        return runtime_file
    if runtime_dir is None:
        return None
    canonical = runtime_dir / "process-resources.json"
    if canonical.is_file():
        return canonical
    # Chain-A records have used an explicit suffix while services were being
    # restarted.  Do not silently inspect arbitrary files outside the supplied
    # directory; choose only the newest known process-resources record there.
    candidates = sorted(runtime_dir.glob("process-resources*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else canonical


def load_runtime_identity(runtime_dir: Path | None, runtime_file: Path | None = None) -> dict[str, Any]:
    path = _runtime_file(runtime_dir, runtime_file)
    if path is None:
        return {"provided": False}
    if not path.is_file():
        return {"provided": True, "path": str(path), "exists": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"provided": True, "path": str(path), "exists": True, "parse_error": type(exc).__name__}
    # Keep only non-secret identity and liveness fields; do not copy command
    # lines, environment values, tokens, or paths to credential files.  Both
    # the earlier compact record and Chain A's current explicit record are
    # accepted so a browser run can be tied to the exact service window.
    compact = {
        "provided": True,
        "path": str(path),
        "exists": True,
        "run_id": payload.get("run_id"),
        "project": payload.get("project"),
        "ports": payload.get("ports"),
        "endpoints": payload.get("endpoints"),
        "pids": payload.get("pids"),
        "checks": payload.get("checks"),
        "compose": {
            "exit_code": (payload.get("compose") or {}).get("exit_code"),
            "expected_records_present": (payload.get("compose") or {}).get("expected_records_present"),
        },
        "migration": payload.get("migration"),
        "model_provider": payload.get("model_provider"),
    }
    if payload.get("project_identity") or payload.get("containers") or payload.get("processes"):
        containers = []
        for item in payload.get("containers") or []:
            if not isinstance(item, dict):
                continue
            containers.append({key: item.get(key) for key in ("name", "port", "status") if key in item})
        processes = []
        for item in payload.get("processes") or []:
            if not isinstance(item, dict):
                continue
            processes.append({key: item.get(key) for key in ("service", "launcher_pid", "server_pid", "listen", "workers") if key in item})
        compact.update({
            "schema": "chain-a-process-resources",
            "run_id": payload.get("run_id"),
            "project": payload.get("project_identity"),
            "containers": containers,
            "processes": processes,
            "health": payload.get("health"),
            "secret_values_recorded": payload.get("secret_values_recorded"),
        })
    return compact


def http_probe(url: str, timeout: float = 3.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "text/html,application/json"}, method="GET")
    started = datetime.now(timezone.utc)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096)
            return {
                "url": url,
                "ok": True,
                "status": response.status,
                "content_type": response.headers.get("Content-Type"),
                "body_prefix_sha256": hashlib.sha256(body).hexdigest(),
                "elapsed_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 1),
            }
    except urllib.error.HTTPError as exc:
        body = exc.read(4096)
        return {
            "url": url,
            "ok": False,
            "status": exc.code,
            "body_prefix_sha256": hashlib.sha256(body).hexdigest(),
            "error": "HTTPError",
            "elapsed_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 1),
        }
    except (OSError, urllib.error.URLError) as exc:
        return {
            "url": url,
            "ok": False,
            "error": type(exc).__name__,
            "elapsed_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 1),
        }


def initial_matrix(args: argparse.Namespace, manifest: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "p10-07-browser-matrix-v1",
        "run_id": args.run_id,
        "created_at": utc_now(),
        "working_directory": str(ROOT),
        "git": git_identity(),
        "source_manifest": {"sha256": manifest["sha256"], "file_count": manifest["file_count"]},
        "browser_matrix": {
            "chromium": {"required": True, "status": "not_executed", "browser_version": None},
            "edge": {"required": True, "status": "not_executed", "browser_version": None},
        },
        "target": {
            "base_url": args.base_url,
            "api_url": args.api_url,
            "runtime_identity": runtime,
            "credentials": "not recorded; provide through isolated browser fixture",
        },
        "evidence_policy": {
            "real_layer": "actual frontend -> API -> queue/Worker -> MCP/MinIO/model where required",
            "deterministic_layer": "controlled fault or stable API fixture; never counted as real business success",
            "route_fulfill": "forbidden as evidence of a real chain",
            "trace_location": "under this run browser/ directory only; inspect before sharing",
            "download_checks": ["actual byte length", "sha256", "MIME", "format structure/content"],
        },
        "cases": [
            {
                **asdict(case),
                "requires": list(case.requires),
                "expected_observations": list(case.expected_observations),
                "status": "not_executed",
                "observed_at": None,
                "browser": None,
                "actual_layer": None,
                "notes": [],
                "artifacts": [],
            }
            for case in CASES
        ],
    }


def summary(matrix: dict[str, Any]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    layers: dict[str, int] = {}
    for case in matrix.get("cases", []):
        statuses[case["status"]] = statuses.get(case["status"], 0) + 1
        if case.get("actual_layer"):
            layers[case["actual_layer"]] = layers.get(case["actual_layer"], 0) + 1
    return {
        "schema_version": "p10-07-browser-summary-v1",
        "run_id": matrix.get("run_id"),
        "updated_at": utc_now(),
        "status_counts": statuses,
        "actual_layer_counts": layers,
        "browser_matrix": matrix.get("browser_matrix", {}),
    }


def ensure_run(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    directory = run_dir(args.run_id)
    matrix_path = directory / "matrix.json"
    if not matrix_path.is_file():
        manifest = write_source_manifest(directory)
        runtime = load_runtime_identity(
            Path(args.runtime_dir) if args.runtime_dir else None,
            Path(args.runtime_file) if args.runtime_file else None,
        )
        matrix = initial_matrix(args, manifest, runtime)
        json_dump(directory / "manifest.json", manifest)
        json_dump(matrix_path, matrix)
        json_dump(directory / "summary.json", summary(matrix))
        (directory / "README.md").write_text(
            "# P10-07 browser evidence run\n\n"
            f"- run_id: `{args.run_id}`\n"
            f"- created_at: `{matrix['created_at']}`\n"
            f"- source_manifest_sha256: `{manifest['sha256']}`\n\n"
            "Use `matrix.json` for the case contract. Place screenshots, traces, "
            "network summaries and actual downloads below this directory. "
            "Record each outcome with `phase10_browser_matrix.py ... record`; "
            "never put credentials or bearer tokens in this directory.\n",
            encoding="utf-8",
        )
    else:
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    return directory, matrix


def command_init(args: argparse.Namespace) -> int:
    directory, matrix = ensure_run(args)
    print(json.dumps({"run_dir": str(directory), "matrix": str(directory / "matrix.json"), "summary": summary(matrix)}, ensure_ascii=False, indent=2))
    return 0


def command_preflight(args: argparse.Namespace) -> int:
    directory, matrix = ensure_run(args)
    probes = [http_probe(args.base_url.rstrip("/"))]
    if args.health_url:
        probes.append(http_probe(args.health_url))
    api_probe = http_probe(args.api_url.rstrip("/") + "/health/ready") if args.api_url else None
    if api_probe:
        probes.append(api_probe)
    frontend_ok = bool(probes and probes[0].get("ok"))
    api_ok = bool(api_probe and api_probe.get("ok") and 200 <= int(api_probe.get("status", 0)) < 300)
    result = {
        "schema_version": "p10-07-browser-preflight-v1",
        "run_id": args.run_id,
        "checked_at": utc_now(),
        "browser_matrix": matrix["browser_matrix"],
        "target": matrix["target"],
        "probes": probes,
        "ready_for_real_browser": frontend_ok and api_ok,
        "ready_conditions": {"frontend_root_2xx": frontend_ok, "api_health_2xx": api_ok},
        "interpretation": "HTTP reachability only; does not prove login, queue, Worker, MCP, MinIO, model, or business correctness",
    }
    json_dump(directory / "preflight.json", result)
    json_dump(directory / "summary.json", summary(matrix))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_refresh(args: argparse.Namespace) -> int:
    """Refresh source/runtime identity without erasing recorded case outcomes."""
    directory, matrix = ensure_run(args)
    manifest = write_source_manifest(directory)
    runtime = load_runtime_identity(
        Path(args.runtime_dir) if args.runtime_dir else None,
        Path(args.runtime_file) if args.runtime_file else None,
    )
    matrix["source_manifest"] = {"sha256": manifest["sha256"], "file_count": manifest["file_count"]}
    existing_ids = {case.get("case_id") for case in matrix.get("cases", [])}
    for definition in CASES:
        if definition.case_id in existing_ids:
            continue
        matrix.setdefault("cases", []).append({
            **asdict(definition),
            "requires": list(definition.requires),
            "expected_observations": list(definition.expected_observations),
            "status": "not_executed",
            "observed_at": None,
            "browser": None,
            "actual_layer": None,
            "notes": [],
            "artifacts": [],
        })
    matrix.setdefault("target", {}).update({
        "base_url": args.base_url,
        "api_url": args.api_url,
        "runtime_identity": runtime,
    })
    json_dump(directory / "manifest.json", manifest)
    json_dump(directory / "matrix.json", matrix)
    json_dump(directory / "summary.json", summary(matrix))
    print(json.dumps({"run_dir": str(directory), "matrix": str(directory / "matrix.json"), "runtime_identity": runtime, "source_manifest": matrix["source_manifest"]}, ensure_ascii=False, indent=2))
    return 0


def find_case(matrix: dict[str, Any], case_id: str) -> dict[str, Any]:
    for case in matrix.get("cases", []):
        if case.get("case_id") == case_id:
            return case
    raise SystemExit(f"unknown case id: {case_id}")


def command_record(args: argparse.Namespace) -> int:
    directory, matrix = ensure_run(args)
    case = find_case(matrix, args.case_id)
    if args.status not in {"passed", "failed", "skipped", "not_executed", "blocked"}:
        raise SystemExit("status must be passed, failed, skipped, not_executed, or blocked")
    if args.layer not in {"real", "deterministic", "none"}:
        raise SystemExit("layer must be real, deterministic, or none")
    if args.browser not in {"chromium", "edge", "both", None}:
        raise SystemExit("browser must be chromium, edge, both, or omitted")
    case["status"] = args.status
    case["observed_at"] = utc_now()
    case["browser"] = args.browser
    case["actual_layer"] = None if args.layer == "none" else args.layer
    if args.browser_version:
        case["browser_version"] = args.browser_version
        browser_names = ("chromium", "edge") if args.browser == "both" else ((args.browser,) if args.browser else ())
        for browser_name in browser_names:
            matrix.setdefault("browser_matrix", {}).setdefault(browser_name, {})["browser_version"] = args.browser_version
    if args.note:
        case.setdefault("notes", []).append(args.note)
    for artifact in args.artifact:
        if not SAFE_ARTIFACT_RE.fullmatch(artifact) or artifact.startswith(("/", "\\")) or ".." in Path(artifact).parts:
            raise SystemExit(f"artifact must be a safe run-relative path: {artifact!r}")
        case.setdefault("artifacts", []).append(artifact.replace("\\", "/"))
    json_dump(directory / "matrix.json", matrix)
    json_dump(directory / "summary.json", summary(matrix))
    print(json.dumps({"case": case, "summary": summary(matrix)}, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=f"p10-07-browser-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--api-url", default="http://127.0.0.1:28080")
    parser.add_argument("--health-url", default="http://127.0.0.1:5173/health/ready")
    parser.add_argument("--runtime-dir", default=os.environ.get("P10_RUNTIME_DIR"))
    parser.add_argument("--runtime-file", default=os.environ.get("P10_RUNTIME_FILE"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create an isolated run and matrix")
    sub.add_parser("preflight", help="record URL/health reachability and runtime identity")
    sub.add_parser("refresh", help="refresh source/runtime identity while preserving case outcomes")
    record = sub.add_parser("record", help="record one browser case result")
    record.add_argument("case_id")
    record.add_argument("status")
    record.add_argument("--browser", choices=("chromium", "edge", "both"))
    record.add_argument("--browser-version")
    record.add_argument("--layer", choices=("real", "deterministic", "none"), default="none")
    record.add_argument("--note")
    record.add_argument("--artifact", action="append", default=[])
    return parser


def main() -> int:
    args = parser().parse_args()
    if args.command == "init":
        return command_init(args)
    if args.command == "preflight":
        return command_preflight(args)
    if args.command == "refresh":
        return command_refresh(args)
    if args.command == "record":
        return command_record(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    sys.exit(main())
