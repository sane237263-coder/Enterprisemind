"""Regression controls for effective build exclusions and redacted findings."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import security_release_check as scanner
from build_context_check import IgnoreRules, check_build_context

PROJECT = Path(__file__).resolve().parents[2]


class BuildContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dockerfile = self.root / "deploy/backend/Dockerfile"
        self.write(
            "deploy/backend/Dockerfile", "FROM python:3.13\nCOPY backend/src /app\nUSER app\n"
        )
        self.write("backend/src/app.py", "print('application')\n")
        self.write(".dockerignore", (PROJECT / ".dockerignore").read_text(encoding="utf-8"))

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def kinds(self):
        findings, _ = check_build_context(self.root, self.dockerfile)
        return {finding["kind"] for finding in findings}

    def test_real_repository_contexts(self):
        for dockerfile in (PROJECT / "deploy").glob("**/Dockerfile"):
            with self.subTest(dockerfile=dockerfile):
                findings, coverage = check_build_context(PROJECT, dockerfile)
                self.assertEqual(findings, [])
                self.assertEqual(coverage["ignorefile"], ".dockerignore")

    def test_legacy_root_excluded_frontend_despite_deploy_rules(self):
        self.write(".dockerignore", "**\n!backend/\n!backend/src/**\n")
        self.write("deploy/.dockerignore", "secrets/\n*.key\n.local\n.env\n")
        self.write("frontend/package.json", "{}")
        self.write(
            "deploy/backend/Dockerfile",
            "FROM node:22\nCOPY frontend/package.json /app/\nUSER app\n",
        )
        self.assertIn("build_input_excluded", self.kinds())

    def test_dockerfile_specific_rules_take_precedence(self):
        self.write("deploy/backend/Dockerfile.dockerignore", "# permissive override\n")
        findings, coverage = check_build_context(self.root, self.dockerfile)
        self.assertEqual(coverage["ignorefile"], "deploy/backend/Dockerfile.dockerignore")
        self.assertIn("sensitive_build_path_included", {item["kind"] for item in findings})

    def test_nested_secrets_are_excluded_even_in_allowed_trees(self):
        rules = IgnoreRules((self.root / ".dockerignore").read_text())
        for path in (
            "frontend/public/nested/tls.key",
            "backend/src/nested/.env",
            "frontend/src/secrets/key",
        ):
            self.assertFalse(rules.includes(path), path)
        self.assertTrue(rules.includes("backend/src/enterprise_mind/prompts/agent/plan.txt"))

    def test_ignore_last_matching_rule_wins(self):
        rules = IgnoreRules("**\n!backend/**\n**/*.key\n!backend/allowed.key\n")
        self.assertFalse(rules.includes("backend/nested/secret.key"))
        self.assertTrue(rules.includes("backend/allowed.key"))

    def test_unsupported_ignore_rule_is_a_failure(self):
        self.write(".dockerignore", "[abc]\n")
        self.assertIn("dockerignore_missing_or_unsupported", self.kinds())

    def test_missing_copy_source(self):
        self.write("deploy/backend/Dockerfile", "FROM python:3.13\nCOPY absent /app\nUSER app\n")
        self.assertIn("copy_source_missing", self.kinds())

    def test_arg_scope_and_copy_from_regressions(self):
        self.write(
            "deploy/backend/Dockerfile",
            "ARG PYTHON_IMAGE\nFROM ${PYTHON_IMAGE}\nARG UV_IMAGE\n"
            "COPY --from=${UV_IMAGE} /uv /bin/uv\n"
            "ARG NGINX_IMAGE\nFROM ${NGINX_IMAGE}\nUSER app\n",
        )
        self.assertTrue({"copy_from_variable_unsupported", "from_arg_not_global"} <= self.kinds())

    def test_frontend_npm_install_directory(self):
        self.write("frontend/package-lock.json", "{}")
        self.write(
            "deploy/backend/Dockerfile",
            "FROM node:22\nWORKDIR /src\nCOPY frontend/package-lock.json ./frontend/\n"
            "RUN npm ci --ignore-scripts\nUSER app\n",
        )
        self.assertIn("npm_install_directory_mismatch", self.kinds())

    def test_scan_redacts_marker_and_covers_application_source(self):
        token = "sk-" + "a" * 30
        self.write("backend/src/app.py", "TOKEN = '" + token + "'\n")
        self.write(
            "deploy/compose.yaml",
            "build:\n  context: ..\n  dockerfile: deploy/backend/Dockerfile\n"
            "ports: ${EM_EDGE_BIND:?}\n",
        )
        with patch.object(scanner, "ROOT", self.root):
            result = scanner.scan(argparse.Namespace(output=None))
        self.assertEqual(result["code_config_scan"], "FAILED")
        self.assertIn("credential_marker", {item["kind"] for item in result["findings"]})
        self.assertNotIn(token, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
