"""Integration of mount paths, tokenizer bases and the runtime UID probe."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import preflight
from provider_contract_check import check_provider_contract


def providers_text(relative: str, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return (
        "llm: {token_counter: utf8_bytes}\n"
        f"embedding: {{tokenizer_file: '{relative}', tokenizer_sha256: '{digest}'}}\n"
    )


class ConfigAccessIntegrationTests(unittest.TestCase):
    def test_preflight_checks_effective_application_config_before_accepting_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tokenizer.json").write_bytes(b"synthetic tokenizer")
            (root / "database-ca.pem").write_bytes(b"synthetic CA wiring fixture")
            configs = {
                "database.yaml": "{}",
                "database-mcp-server.yaml": (
                    "database:\n  sslmode: verify-full\n"
                    "  connection_options:\n    target_session_attrs: read-only\n"
                ),
                "rag.yaml": "{}",
                "providers.yaml": providers_text("tokenizer.json", b"synthetic tokenizer"),
            }
            for name, text in configs.items():
                (root / name).write_text(text, encoding="utf-8")
            for application, error in (
                ("api:\n  workers: 1\napi:\n  workers: 2\n", "application config must pin"),
                ("api: {workers: 1}\n", None),
                (
                    "api:\n  workers: 1\nprivate-field: synthetic-private-value\n",
                    "application config does not match the runtime schema",
                ),
            ):
                with self.subTest(application=application):
                    (root / "application.yaml").write_text(application, encoding="utf-8")
                    result = preflight.CheckResult()
                    with (
                        patch.dict(preflight.os.environ, {"EM_CONFIG_DIR": str(root)}, clear=True),
                        patch.object(preflight, "check_secret_files", return_value=[]),
                        patch.object(preflight, "check_tls_contract", return_value=[]),
                        patch.object(preflight, "check_database_ca", return_value=[]),
                        patch.object(preflight, "check_config_access", return_value=[]),
                    ):
                        preflight._check_paths(result, static=False)
                    application_errors = [e for e in result.errors if e.startswith("application")]
                    if error is None:
                        self.assertEqual(application_errors, [])
                    else:
                        self.assertTrue(any(e.startswith(error) for e in application_errors))
                    self.assertEqual(
                        "release configs and tokenizers are readable as UID/GID 65532"
                        in result.passed,
                        error is None,
                    )
                    self.assertNotIn("synthetic-private-value", " ".join(result.errors))

    def test_tokenizer_base_matches_resolved_provider_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            nested = root / "bundle"
            nested.mkdir()
            provider = nested / "providers.yaml"
            provider.write_text("{}", encoding="utf-8")
            (nested / "tokenizer.json").write_bytes(b"nested bytes")
            (root / "tokenizer.json").write_bytes(b"root bytes")
            for raw, content, parts in (
                ("tokenizer.json", b"nested bytes", ("bundle", "tokenizer.json")),
                ("/run/config/tokenizer.json", b"root bytes", ("tokenizer.json",)),
            ):
                with self.subTest(path=raw):
                    paths = {}
                    errors, hashes = check_provider_contract(
                        providers_text(raw, content),
                        root,
                        providers_path=provider,
                        tokenizer_paths=paths,
                    )
                    self.assertEqual(errors, [])
                    self.assertEqual(paths, {"tokenizer/embedding": parts})
                    self.assertEqual(
                        hashes["tokenizer/embedding"], hashlib.sha256(content).hexdigest()
                    )

    def test_preflight_passes_yaml_and_tokenizer_paths_to_uid_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tokenizer.json").write_bytes(b"synthetic tokenizer")
            (root / "database-ca.pem").write_bytes(b"synthetic CA wiring fixture")
            configs = {
                "application.yaml": "api:\n  workers: 1\n",
                "database.yaml": "{}",
                "database-mcp-server.yaml": (
                    "database:\n  sslmode: verify-full\n"
                    "  connection_options:\n    target_session_attrs: read-only\n"
                ),
                "rag.yaml": "{}",
                "providers.yaml": providers_text("tokenizer.json", b"synthetic tokenizer"),
            }
            for name, text in configs.items():
                (root / name).write_text(text, encoding="utf-8")
            for errors, ca_errors in (
                ([], []),
                (["config/application.yaml is unreadable as UID/GID 65532"], []),
                ([], ["database CA bundle is missing or invalid"]),
            ):
                with self.subTest(probe_errors=errors, ca_errors=ca_errors):
                    result = preflight.CheckResult()
                    with (
                        patch.dict(preflight.os.environ, {"EM_CONFIG_DIR": str(root)}, clear=True),
                        patch.object(preflight, "check_secret_files", return_value=[]),
                        patch.object(preflight, "check_tls_contract", return_value=[]),
                        patch.object(preflight, "check_database_ca", return_value=ca_errors),
                        patch.object(
                            preflight, "check_config_access", return_value=errors
                        ) as probe,
                    ):
                        hashes = preflight._check_paths(result, static=False)
                    probe.assert_called_once_with(
                        root,
                        {
                            **{f"config/{name}": (name,) for name in configs},
                            "tokenizer/embedding": ("tokenizer.json",),
                            "config/database-ca.pem": ("database-ca.pem",),
                        },
                    )
                    self.assertIn("tokenizer/embedding", hashes)
                    self.assertEqual(
                        "release configs and tokenizers are readable as UID/GID 65532"
                        in result.passed,
                        not errors and not ca_errors,
                    )
                    self.assertEqual("config/database-ca.pem" in hashes, not ca_errors)
                    for error in errors + ca_errors:
                        self.assertIn(error, result.errors)

    def test_preflight_does_not_read_yaml_after_mount_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = preflight.CheckResult()
            with (
                patch.dict(preflight.os.environ, {"EM_CONFIG_DIR": temporary}, clear=True),
                patch.object(preflight, "check_secret_files", return_value=[]),
                patch.object(preflight, "check_tls_contract", return_value=[]),
                patch.object(preflight, "check_config_access", return_value=[]),
                patch.object(
                    preflight, "resolve_config_file", side_effect=ValueError("private-path")
                ),
                patch.object(preflight, "_sha256") as digest,
                patch.object(Path, "read_text") as read,
            ):
                hashes = preflight._check_paths(result, static=False)
            self.assertEqual(hashes, {})
            digest.assert_not_called()
            read.assert_not_called()
            self.assertNotIn("private-path", " ".join(result.errors))
            self.assertNotIn(
                "release configs and tokenizers are readable as UID/GID 65532", result.passed
            )
            self.assertEqual(
                sum("must resolve within its container mount" in e for e in result.errors), 5
            )

    def test_static_mode_never_resolves_or_probes_config_files(self):
        with (
            patch.object(preflight, "resolve_config_file") as resolve,
            patch.object(preflight, "check_config_access") as probe,
        ):
            preflight._check_paths(preflight.CheckResult(), static=True)
        resolve.assert_not_called()
        probe.assert_not_called()

    def test_config_root_resolution_failure_is_redacted_and_blocked(self):
        result = preflight.CheckResult()
        with (
            patch.dict(preflight.os.environ, {"EM_CONFIG_DIR": "/synthetic-config"}, clear=True),
            patch.object(Path, "resolve", side_effect=OSError("private-path-diagnostic")),
            patch.object(preflight, "check_secret_files", return_value=[]),
            patch.object(preflight, "check_tls_contract", return_value=[]),
            patch.object(preflight, "check_config_access") as probe,
        ):
            preflight._check_paths(result, static=False)
        self.assertIn("EM_CONFIG_DIR does not identify a directory", result.errors)
        self.assertNotIn("private-path-diagnostic", " ".join(result.errors))
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
