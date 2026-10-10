"""Bounded release scan for P10-10 security evidence.

The scan is deliberately local and deterministic. It checks candidate source
and deployment materials for obvious secret material, image/version hazards,
and build-context exclusions. It does not turn an unavailable registry or
vendor advisory service into a clean security result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from build_context_check import CONFIG_FILES, check_build_context

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
ACCESS_TOKEN = re.compile(r"(?:sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})")
LATEST = re.compile(r"(^|\s|:)latest(?:\s|$|['\"])")


def candidate_files() -> list[Path]:
    paths = [
        ROOT / ".dockerignore",
        ROOT / "backend" / "pyproject.toml",
        ROOT / "backend" / "uv.lock",
        ROOT / "frontend" / "package-lock.json",
        ROOT / "frontend" / "package.json",
        ROOT / "deploy" / "compose.yaml",
        ROOT / "deploy" / "backup" / "compose.restore.override.yaml",
        ROOT / "deploy" / ".env.example",
        ROOT / "deploy" / ".dockerignore",
        ROOT / "deploy" / "frontend" / "nginx.conf",
    ]
    paths.extend((ROOT / "deploy").glob("**/Dockerfile"))
    paths.extend((ROOT / "deploy" / "config").glob("*.yaml"))
    paths.extend((ROOT / "deploy").glob("**/*.py"))
    paths.extend((ROOT / "deploy").glob("**/*.sh"))
    paths.extend(ROOT / "config" / name for name in CONFIG_FILES)
    for directory in ("backend/src", "backend/migrations", "frontend/src", "frontend/dist"):
        paths.extend(
            path
            for path in (ROOT / directory).rglob("*")
            if path.suffix
            in {".py", ".txt", ".json", ".ts", ".tsx", ".js", ".css", ".html", ".map"}
            and "__pycache__" not in path.parts
        )
    return sorted({path for path in paths if path.is_file()})


def scan(args: argparse.Namespace) -> dict[str, object]:
    files = candidate_files()
    findings: list[dict[str, object]] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = str(path.relative_to(ROOT))
        for line_number, line in enumerate(text.splitlines(), start=1):
            if PRIVATE_KEY.search(line) or ACCESS_TOKEN.search(line):
                findings.append(
                    {"file": relative, "line": line_number, "kind": "credential_marker"}
                )
            image_config_file = (
                path.name == "Dockerfile"
                or path.suffix in {".yaml", ".yml", ".toml"}
                or path.name == ".env.example"
            )
            if image_config_file and not line.lstrip().startswith("#") and LATEST.search(line):
                findings.append(
                    {
                        "file": relative,
                        "line": line_number,
                        "kind": "floating_image_tag",
                    }
                )

    compose = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    if "EM_EDGE_BIND:?" not in compose:
        findings.append({"kind": "edge_bind_not_explicit"})
    contexts = []
    declared_contexts = re.findall(r"^\s*context: (.+)$", compose, re.M)
    if not declared_contexts or any(value.strip() != ".." for value in declared_contexts):
        findings.append({"kind": "non_root_build_context_not_checked"})
    dockerfiles = sorted((ROOT / "deploy").glob("**/Dockerfile"))
    declared = set(re.findall(r"^\s*dockerfile: (.+)$", compose, re.M))
    if declared != {path.relative_to(ROOT).as_posix() for path in dockerfiles}:
        findings.append({"kind": "compose_dockerfile_coverage_mismatch"})
    for dockerfile in dockerfiles:
        context_findings, coverage = check_build_context(ROOT, dockerfile)
        findings.extend(context_findings)
        contexts.append(coverage)

    result: dict[str, object] = {
        "code_config_scan": "PASS" if not findings else "FAILED",
        "candidate_file_count": len(files),
        "candidate_files": [path.relative_to(ROOT).as_posix() for path in files],
        "findings": findings,
        "build_contexts": contexts,
        "frontend_bundle_scan": "SNAPSHOT_SCANNED"
        if (ROOT / "frontend/dist/index.html").exists()
        else "NOT_PRESENT",
        "frontend_bundle_sha256": {
            path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files
            if path.is_relative_to(ROOT / "frontend/dist")
        },
        "docker_build": "NOT_EXECUTED_STATIC_CHECK_ONLY",
        "dependency_and_image_advisories": "NOT_EXECUTED_BY_THIS_LOCAL_SCANNER",
        "scope": "Obvious credential markers and repository-specific static build checks only",
        "production_security_acceptance": False,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = scan(args)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["code_config_scan"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
