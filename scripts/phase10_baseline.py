"""Create a reproducible Phase-10 source baseline without reading secret material.

The repository contains a large amount of intentionally untracked source.  This
script hashes the source, tests, migrations, deployment files, configuration
templates, locks and documentation that form a candidate release.  It never
walks ``.local`` or virtual-environment/cache directories, and it records the
candidate as ``HEAD + source_manifest_sha256`` instead of pretending that the
working tree is a clean commit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

EXCLUDED_DIRS = {
    ".git",
    ".local",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}
EXCLUDED_PREFIXES = ("pytest-", "phase9-manual-")
SAFE_ROOTS = (
    "backend/src",
    "backend/tests",
    "backend/migrations",
    "frontend/src",
    "frontend/tests",
    "deploy",
    "scripts",
    "config",
    "docs",
)
SAFE_ROOT_FILES = (
    ".dockerignore",
    ".env.example",
    ".gitignore",
    ".python-version",
    "README.md",
    "compose.test.yaml",
    "backend/alembic.ini",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "frontend/index.html",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/tsconfig.json",
    "frontend/vite.config.ts",
)
# This is the sole generated document that must stay outside the manifest. It
# records the manifest hash itself, so including it would make each write alter
# the next hash and prevent a reproducible candidate from being identified.
EXCLUDED_MANIFEST_FILES = {
    Path("docs") / "第十阶段实施与验收记录.md",
}


def run_git(root: Path, *args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    return result.returncode, result.stdout, result.stderr


def is_excluded(relative: Path) -> bool:
    parts = relative.parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    return any(
        part.startswith(prefix) for part in parts for prefix in EXCLUDED_PREFIXES
    )


def is_secret_material(relative: Path) -> bool:
    name = relative.name.lower()
    if name in {
        ".env",
        "redis.acl",
        "postgres_password",
        "business_password",
        "redis_password",
        "redis_test_admin",
        "token_key",
        "admin_password",
        "minio_root_access",
        "minio_root_secret",
        "minio_app_access",
        "minio_app_secret",
        "phase10_business_readonly_password",
        "phase10_mcp_token",
    }:
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    # Local provider/database overrides are deliberately ignored by Git and
    # may contain endpoint credentials.  The checked-in null/default and
    # *.example templates remain in the baseline.
    if name.endswith((".local.yaml", ".local.yml", ".local.json", ".local.ps1")):
        return True
    if name.endswith((".secret.yaml", ".secret.yml", ".secret.json", ".secret.ps1")):
        return True
    return name.endswith((".pem", ".key", ".p12", ".pfx"))


def is_manifest_excluded(relative: Path) -> bool:
    return relative in EXCLUDED_MANIFEST_FILES


def iter_files(root: Path) -> Iterable[Path]:
    seen: set[Path] = set()
    for relative_root in SAFE_ROOTS:
        path = root / relative_root
        if not path.exists():
            continue
        candidates = path.rglob("*") if path.is_dir() else (path,)
        for candidate in candidates:
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(root)
            if (
                is_excluded(relative)
                or is_manifest_excluded(relative)
                or is_secret_material(relative)
            ):
                continue
            if relative not in seen:
                seen.add(relative)
                yield relative
    for relative_name in SAFE_ROOT_FILES:
        relative = Path(relative_name)
        path = root / relative
        if (
            path.is_file()
            and not is_excluded(relative)
            and not is_manifest_excluded(relative)
            and not is_secret_material(relative)
            and relative not in seen
        ):
            seen.add(relative)
            yield relative


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    root = args.root.resolve()
    if not (root / "backend" / "pyproject.toml").is_file():
        raise SystemExit(f"not an EnterpriseMind root: {root}")
    if any(char in args.run_id for char in "\\/:\r\n") or not args.run_id.strip():
        raise SystemExit("run-id must be a non-empty path-safe value")

    evidence = root / ".local" / "phase10" / args.run_id / "baseline"
    evidence.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC).isoformat()
    code, head_out, head_err = run_git(root, "rev-parse", "HEAD")
    if code != 0 or not head_out.strip():
        raise SystemExit(f"cannot read git HEAD: {head_err.strip()}")
    head = head_out.strip()
    _, branch_out, _branch_err = run_git(root, "branch", "--show-current")
    branch = branch_out.strip() or "(detached)"
    status_code, status_out, status_err = run_git(
        root, "status", "--porcelain=v1", "--untracked-files=all"
    )
    (evidence / "git-status.txt").write_text(status_out, encoding="utf-8")
    (evidence / "git-status.stderr.txt").write_text(status_err, encoding="utf-8")
    untracked_code, untracked_out, untracked_err = run_git(
        root, "ls-files", "--others", "--exclude-standard"
    )

    entries: list[tuple[str, str, int]] = []
    errors: list[str] = []
    for relative in sorted(iter_files(root), key=lambda item: item.as_posix()):
        try:
            digest, size = sha256_file(root / relative)
        except OSError as error:
            errors.append(f"{relative.as_posix()}\t{type(error).__name__}: {error}")
            continue
        entries.append((relative.as_posix(), digest, size))
    manifest_lines = [f"{path}\t{digest}\t{size}" for path, digest, size in entries]
    manifest_text = "\n".join(manifest_lines) + ("\n" if manifest_lines else "")
    manifest_hash = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
    (evidence / "source-manifest.tsv").write_text(manifest_text, encoding="utf-8")
    (evidence / "source-manifest.sha256").write_text(
        manifest_hash + "\n", encoding="utf-8"
    )
    if errors:
        (evidence / "manifest-errors.txt").write_text(
            "\n".join(errors) + "\n", encoding="utf-8"
        )

    allowed_untracked: list[str] = []
    for line in untracked_out.splitlines():
        relative = Path(line.strip())
        if (
            not line.strip()
            or is_excluded(relative)
            or is_manifest_excluded(relative)
            or is_secret_material(relative)
        ):
            continue
        if (
            any(
                relative == Path(name) or relative.is_relative_to(Path(name))
                for name in SAFE_ROOTS
            )
            or relative.as_posix() in SAFE_ROOT_FILES
        ):
            allowed_untracked.append(relative.as_posix())
    (evidence / "untracked-source.txt").write_text(
        "\n".join(sorted(allowed_untracked)) + ("\n" if allowed_untracked else ""),
        encoding="utf-8",
    )
    (evidence / "untracked-source.stderr.txt").write_text(
        untracked_err, encoding="utf-8"
    )
    candidate = f"HEAD={head}\nsource_manifest_sha256={manifest_hash}\n"
    (evidence / "candidate-version.txt").write_text(candidate, encoding="utf-8")
    exclusions = [
        ".git, .local, virtual environments, node_modules and tool caches",
        "secret files, .env files other than .env.example, and private model files",
        "legacy pytest/phase9 manual scratch directories",
        "docs/第十阶段实施与验收记录.md (generated self-referential acceptance record)",
    ]
    (evidence / "manifest-exclusions.txt").write_text(
        "\n".join(exclusions) + "\n", encoding="utf-8"
    )
    inventory = {
        "run_id": args.run_id,
        "generated_at_utc": generated_at,
        "root": str(root),
        "branch": branch,
        "head": head,
        "candidate": {
            "head": head,
            "source_manifest_sha256": manifest_hash,
        },
        "manifest": {
            "path": "baseline/source-manifest.tsv",
            "file_count": len(entries),
            "untracked_source_count": len(allowed_untracked),
            "errors": len(errors),
        },
        "git": {
            "status_exit_code": status_code,
            "untracked_exit_code": untracked_code,
            "head_exit_code": code,
        },
    }
    (evidence / "inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(inventory, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
