"""Static checks for this repository's root-context Dockerfiles (not a build).

Only the literal COPY paths and simple ignore globs used here are supported.
Unsupported syntax is a finding, never silently treated as passing.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path, PurePosixPath

CONFIG_FILES = (
    "agent.yaml",
    "application.yaml",
    "database.yaml",
    "providers.yaml",
    "rag.yaml",
    "tokenizers.yaml",
)
SENSITIVE_PROBES = (
    ".local/evidence.json",
    ".git/config",
    "deploy/.env",
    "secrets/token",
    "deploy/secrets/token",
    "config/providers.local.yaml",
    "config/rag.local.yaml",
    "backend/.venv/pyvenv.cfg",
    "frontend/node_modules/package/index.js",
    "frontend/dist/index.html",
    "backend/tests/test_example.py",
    "frontend/src/.env.production",
    "backend/src/nested/secrets/token",
    "frontend/public/tls.key",
    "frontend/public/tls.pem",
    "frontend/public/tls.pfx",
    "backend/src/nested/settings.local.yaml",
    "backend/src/nested/key.secret.txt",
    "backend/src/nested/__pycache__/module.pyc",
)


class IgnoreRules:
    def __init__(self, text: str):
        self.rules: list[tuple[bool, str]] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            include = line.startswith("!")
            pattern = line.removeprefix("!").strip("/")
            if not pattern or any(char in pattern for char in "\\[]"):
                raise ValueError("unsupported ignore syntax")
            if any(part in {".", ".."} for part in pattern.split("/")):
                raise ValueError("unsupported relative ignore syntax")
            self.rules.append((include, pattern))

    def includes(self, relative: str) -> bool:
        path = PurePosixPath(relative)
        candidates = [path, *[parent for parent in path.parents if str(parent) != "."]]
        included = True
        for include, pattern in self.rules:
            if any(item.full_match(pattern, case_sensitive=True) for item in candidates):
                included = include
        return included


def required_inputs(root: Path) -> set[Path]:
    fixed = (
        "backend/pyproject.toml",
        "backend/uv.lock",
        "backend/alembic.ini",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/index.html",
        "frontend/tsconfig.json",
        "frontend/vite.config.ts",
        "deploy/frontend/nginx.conf",
    )
    paths = {root / item for item in fixed}
    paths.update(root / "config" / name for name in CONFIG_FILES)
    for directory in ("backend/src", "backend/migrations", "frontend/src", "frontend/public"):
        for path in (root / directory).rglob("*"):
            if path.is_file() and not any(
                part.startswith(".") for part in path.relative_to(root).parts
            ):
                if "__pycache__" not in path.parts and path.suffix != ".pyc":
                    paths.add(path)
    return paths


def check_build_context(root: Path, dockerfile: Path) -> tuple[list[dict], dict]:
    findings: list[dict] = []
    relative = dockerfile.relative_to(root).as_posix()

    def report(kind: str, **details: object) -> None:
        findings.append({"file": relative, "kind": kind, **details})

    specific = dockerfile.with_name(dockerfile.name + ".dockerignore")
    ignorefile = specific if specific.exists() else root / ".dockerignore"
    coverage = {"dockerfile": relative, "ignorefile": ignorefile.relative_to(root).as_posix()}
    try:
        rules = IgnoreRules(ignorefile.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report("dockerignore_missing_or_unsupported")
        return findings, coverage
    for probe in SENSITIVE_PROBES:
        if rules.includes(probe):
            report("sensitive_build_path_included", path=probe)

    text = dockerfile.read_text(encoding="utf-8")
    global_args: set[str] = set()
    first_from = False
    required = required_inputs(root)
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line.startswith("ARG ") and not first_from:
            global_args.add(line.split()[1].split("=")[0])
        if line.startswith("FROM "):
            first_from = True
            for variable in re.findall(r"\$\{(\w+)\}|\$(\w+)", line):
                if next(filter(None, variable)) not in global_args:
                    report("from_arg_not_global", line=number)
        if not line.startswith("COPY "):
            continue
        try:
            tokens = shlex.split(line)[1:]
        except ValueError:
            report("copy_syntax_unsupported", line=number)
            continue
        if any(token.startswith("--from=") for token in tokens):
            if any("$" in token for token in tokens if token.startswith("--from=")):
                report("copy_from_variable_unsupported", line=number)
            continue
        if len(tokens) < 2 or any(
            any(char in token for char in "*$?[]\\")
            or token.startswith("--")
            or ".." in PurePosixPath(token).parts
            or token.startswith("/")
            for token in tokens[:-1]
        ):
            report("copy_syntax_unsupported", line=number)
            continue
        for source in tokens[:-1]:
            path = root / source
            if not path.exists():
                report("copy_source_missing", path=source)
                continue
            inputs = (
                {path}
                if path.is_file()
                else {item for item in required if item.is_relative_to(path)}
            )
            if not inputs:
                report("copy_directory_not_checked", path=source)
            for item in sorted(inputs):
                name = item.relative_to(root).as_posix()
                if not item.exists():
                    report("build_input_missing", path=name)
                elif not rules.includes(name):
                    report("build_input_excluded", path=name)
    last_stage = re.split(r"^FROM\s", text, flags=re.MULTILINE)[-1]
    if not re.search(r"^USER\s+(?!root\b|0(?::|\s|$))[^\s#]+", last_stage, re.MULTILINE):
        report("unprivileged_final_user_missing")
    if "frontend/package-lock.json" in text and re.search(r"^RUN npm ci\b", text, re.M):
        # These Dockerfiles copy manifests under /src/frontend, not WORKDIR /src.
        report("npm_install_directory_mismatch")
    return findings, coverage
