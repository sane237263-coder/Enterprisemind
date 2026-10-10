"""Read-only checks for the files being prepared for a public Git release.

Default: tracked and non-ignored untracked working-tree files.
--staged: index paths, modes and blob contents only, never working-tree contents.
This catches common publication mistakes; it is not a complete secret audit.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

REQUIRED = (
    "README.md",
    "COPYRIGHT.md",
    "assets/chat-demo.png",
    ".gitignore",
    ".gitattributes",
    ".env.example",
    ".python-version",
    "THIRD_PARTY_NOTICES.md",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/src/enterprise_mind/app.py",
    "backend/migrations/env.py",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/src/main.tsx",
    "config/agent.yaml",
    "config/application.yaml",
    "config/database.yaml",
    "config/providers.yaml",
    "config/rag.yaml",
    "config/tokenizers.yaml",
    "config/database.example.yaml",
    "config/providers.example.yaml",
    "config/rag.models.example.yaml",
    "config/model-secrets.example.ps1",
    "config/model-env.example.ps1",
    "compose.test.yaml",
    "deploy/compose.yaml",
    "deploy/.env.example",
    "deploy/README.md",
    "scripts/local-runtime.ps1",
    "scripts/check_backend.py",
    "scripts/check_publication.py",
)
FORBIDDEN_DIRS = {
    ".local",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".vite",
    ".codex-tmp",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "test-results",
    "playwright-report",
    "secrets",
}
TOKENS = (
    ("api_token", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)
PRIVATE_KEY = re.compile(
    r"-----BEGIN ((?:[A-Z0-9]+ )*PRIVATE KEY)-----\s*"
    r"([A-Za-z0-9+/=\s]{64,})-----END \1-----"
)
PERSONAL_PATH = re.compile(
    r"(?:[A-Za-z]:[/\\]+Users[/\\]+[A-Za-z0-9_.-]+[/\\]+"
    r"|/mnt/[a-z]/Users/[A-Za-z0-9_.-]+/|/home/[A-Za-z0-9_.-]+/)"
)
INLINE_LINK = re.compile(
    r'\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+["\'][^\n]*?["\'])?\s*\)'
)
REFERENCE_LINK = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))")


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        input=data,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("git_command_failed")
    return result.stdout


def forbidden(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    example = name.endswith(".example") or ".example." in name
    return (
        parts[0] == "docs"
        or any(part in FORBIDDEN_DIRS for part in parts[:-1])
        or bool(re.match(r"^(?:phase10-manifest-|phase9-manual-)", parts[0]))
        or name.lower().endswith((".pem", ".key", ".p12", ".pfx"))
        or name in {"id_rsa", "id_ed25519"}
        or (
            not example
            and (
                name == ".env"
                or name.startswith(".env.")
                or ".local." in name
                or ".secret." in name
                or ".before-" in name
            )
        )
    )


def markdown_targets(content: str):
    """Recognize inline/image links and reference definitions outside code."""
    fence = None
    for line_number, line in enumerate(content.splitlines(), 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            current = marker[1]
            if fence is None:
                fence = current
            elif current[0] == fence[0] and len(current) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        line = re.sub(r"(`+)(.*?)\1", lambda match: " " * len(match[0]), line)
        for match in INLINE_LINK.finditer(line):
            yield line_number, match[1] or match[2]
        definition = REFERENCE_LINK.match(line)
        if definition:
            yield line_number, definition[1] or definition[2]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--staged", action="store_true", help="Check index contents only"
    )
    parser.add_argument("--max-file-mib", type=int, default=50)
    args = parser.parse_args()
    if args.max_file_mib <= 0:
        parser.error("--max-file-mib must be positive")
    root = args.root.resolve()
    limit = args.max_file_mib * 1024 * 1024
    errors = []
    files = {}
    content_by_path = {}
    scanned_bytes = 0

    def error(code: str, path: str, line: int | None = None) -> None:
        item = {"code": code, "path": path}
        if line is not None:
            item["line"] = line
        errors.append(item)

    try:
        actual_root = Path(
            git(root, "rev-parse", "--show-toplevel").decode().strip()
        ).resolve()
        if actual_root != root:
            raise RuntimeError("root_must_be_repository_root")
        if args.staged:
            for record in git(root, "ls-files", "--stage", "-z").split(b"\0"):
                if not record:
                    continue
                metadata, raw_path = record.split(b"\t", 1)
                mode, oid, stage = metadata.decode().split()
                path = raw_path.decode("utf-8", "surrogateescape")
                if stage != "0":
                    error("unmerged_index", path)
                else:
                    files[path] = (mode, oid)
        else:
            for raw_path in git(
                root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
            ).split(b"\0"):
                if raw_path:
                    files[raw_path.decode("utf-8", "surrogateescape")] = None

        for path in REQUIRED:
            if path not in files:
                error("required_file_missing", path)

        eligible = []
        for path in sorted(files):
            if forbidden(path):
                error("local_or_sensitive_path", path)
                continue
            if args.staged:
                mode, oid = files[path]
                if mode not in {"100644", "100755"}:
                    error("unsupported_index_mode", path)
                else:
                    eligible.append((path, oid))
            else:
                target = root / path
                try:
                    if any(
                        parent.is_symlink()
                        for parent in (target, *target.parents)
                        if parent != root
                    ):
                        error("symlink", path)
                    elif not target.is_file():
                        error("working_file_missing_or_not_regular", path)
                    elif target.stat().st_size > limit:
                        error("file_too_large", path)
                    else:
                        content_by_path[path] = target.read_bytes()
                except OSError:
                    error("file_unreadable", path)

        if args.staged and eligible:
            request = b"".join(oid.encode() + b"\n" for _, oid in eligible)
            info = git(root, "cat-file", "--batch-check", data=request).splitlines()
            selected = []
            for (path, oid), record in zip(eligible, info, strict=True):
                _, kind, size = record.split()
                if kind != b"blob":
                    error("unsupported_index_object", path)
                elif int(size) > limit:
                    error("file_too_large", path)
                else:
                    selected.append((path, oid))
            request = b"".join(oid.encode() + b"\n" for _, oid in selected)
            response = (
                git(root, "cat-file", "--batch", data=request) if selected else b""
            )
            offset = 0
            for path, _ in selected:
                end = response.index(b"\n", offset)
                size = int(response[offset:end].split()[-1])
                start = end + 1
                content_by_path[path] = response[start : start + size]
                offset = start + size + 1

        for path, data in content_by_path.items():
            scanned_bytes += len(data)
            if b"\0" in data:
                continue
            try:
                content = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                continue
            for code, pattern in (*TOKENS, ("private_key", PRIVATE_KEY)):
                for match in pattern.finditer(content):
                    error(code, path, content.count("\n", 0, match.start()) + 1)
            if path.endswith(".md"):
                for match in PERSONAL_PATH.finditer(content):
                    error(
                        "personal_absolute_path",
                        path,
                        content.count("\n", 0, match.start()) + 1,
                    )
                for line, target in markdown_targets(content):
                    parts = urlsplit(target)
                    if parts.scheme or parts.netloc or not parts.path:
                        continue
                    decoded = unquote(parts.path).replace("\\", "/")
                    destination = posixpath.normpath(
                        decoded.lstrip("/")
                        if decoded.startswith("/")
                        else posixpath.join(posixpath.dirname(path), decoded)
                    )
                    exists = destination in files or any(
                        name.startswith(destination.rstrip("/") + "/") for name in files
                    )
                    if (
                        destination == ".."
                        or destination.startswith("../")
                        or not exists
                    ):
                        error("markdown_target_missing", path, line)
    except (OSError, RuntimeError, ValueError) as exception:
        error(
            str(exception)
            if isinstance(exception, RuntimeError)
            else "inspection_failed",
            ".",
        )

    print(
        json.dumps(
            {
                "ok": not errors,
                "mode": "staged" if args.staged else "working-tree",
                "candidate_files": len(files),
                "scanned_files": len(content_by_path),
                "scanned_bytes": scanned_bytes,
                "errors": errors,
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
