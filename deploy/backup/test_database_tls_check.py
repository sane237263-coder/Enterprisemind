"""Offline database TLS preflight and asyncpg parser regressions."""

# ruff: noqa: E402

from __future__ import annotations

import copy
import inspect
import os
import ssl
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml
from asyncpg import connect_utils
from asyncpg.exceptions import ClientConfigurationError
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))

import database_tls_check as database_tls
from runtime_contract_check import check_runtime_contract


class DatabaseTLSCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    @staticmethod
    def valid_config() -> str:
        return """database:
  sslmode: verify-full
  connection_options:
    target_session_attrs: read-only
"""

    def write_certificate(self, name: str, *, ca: bool) -> Path:
        """Create synthetic public test material; the private key stays in memory."""
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "offline database test")])
        now = datetime.now(UTC).replace(tzinfo=None)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        path = self.root / name
        path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        return path

    @staticmethod
    def asyncpg_parser_kwargs() -> dict[str, object]:
        """Supply the installed asyncpg parser's keyword parameters explicitly."""
        signature = inspect.signature(connect_utils._parse_connect_dsn_and_args)
        kwargs = {
            name: parameter.default if parameter.default is not inspect.Parameter.empty else None
            for name, parameter in signature.parameters.items()
        }
        kwargs.update(
            dsn=None,
            host="pg-tls-only.invalid",
            port=5432,
            user="offline-reader",
            password="parser-only-placeholder",
            passfile=None,
            database="offline-database",
            ssl="verify-full",
            service=None,
            servicefile=None,
            direct_tls=None,
            server_settings=None,
            target_session_attrs="read-only",
            krbsrvname=None,
            gsslib=None,
        )
        return kwargs

    def parse_asyncpg(self, *, ca_file: Path | None):
        environment = {"PGSSLROOTCERT": str(ca_file)} if ca_file else {}
        empty_pg_config = self.root / "empty-home" / ".postgresql"
        empty_pg_config.mkdir(parents=True, exist_ok=True)

        def pg_config_path(filename: str) -> Path:
            return empty_pg_config / filename

        # On Windows, OpenSSL needs runtime variables such as SYSTEMROOT. Clear only
        # libpq's PG* overrides instead of replacing the whole process environment.
        with patch.dict(os.environ):
            for name in tuple(os.environ):
                if name.upper().startswith("PG"):
                    os.environ.pop(name)
            os.environ.update(environment)
            with (
                patch.object(connect_utils, "_dot_postgresql_path", side_effect=pg_config_path),
                patch(
                    "socket.create_connection",
                    side_effect=AssertionError("the parser regression must not open a socket"),
                ),
            ):
                return connect_utils._parse_connect_dsn_and_args(**self.asyncpg_parser_kwargs())

    def test_config_accepts_verify_full_and_read_only(self) -> None:
        self.assertEqual(database_tls.check_database_tls_config(self.valid_config()), [])

    def test_config_rejects_disable_and_require_ssl_modes(self) -> None:
        for mode in ("disable", "require"):
            with self.subTest(mode=mode):
                config = self.valid_config().replace("verify-full", mode)
                self.assertEqual(
                    database_tls.check_database_tls_config(config),
                    ["database MCP must require sslmode verify-full"],
                )

    def test_config_rejects_commented_or_mistyped_read_only_setting(self) -> None:
        configs = (
            """database:
  sslmode: verify-full
  # connection_options:
  #   target_session_attrs: read-only
""",
            """database:
  sslmode: verify-full
  connection_options: []
""",
            """database:
  sslmode: verify-full
  connection_options:
    target_session_attrs: true
""",
        )
        for config in configs:
            with self.subTest(config=config):
                self.assertEqual(
                    database_tls.check_database_tls_config(config),
                    ["MCP config must require target_session_attrs: read-only"],
                )

    def test_config_rejects_malformed_or_wrong_top_level_types_without_echoing_input(self):
        for config in ("database: [private-config-marker", "database: []"):
            with self.subTest(config=config):
                errors = database_tls.check_database_tls_config(config)
                self.assertEqual(errors, ["database MCP connection configuration is invalid"])
                self.assertNotIn("private-config-marker", " ".join(errors))

    def test_valid_synthetic_ca_bundle_passes(self) -> None:
        ca_file = self.write_certificate("database-ca.pem", ca=True)

        self.assertEqual(database_tls.check_database_ca(ca_file), [])

    def test_missing_corrupt_and_leaf_ca_bundles_are_rejected_with_fixed_errors(self) -> None:
        missing = self.root / "missing-ca.pem"
        corrupt = self.root / "corrupt-ca.pem"
        corrupt.write_bytes(b"private-ca-bundle-marker")
        leaf = self.write_certificate("leaf.pem", ca=False)

        self.assertEqual(
            database_tls.check_database_ca(missing), ["database CA bundle is missing or invalid"]
        )
        corrupt_errors = database_tls.check_database_ca(corrupt)
        self.assertEqual(corrupt_errors, ["database CA bundle is missing or invalid"])
        self.assertNotIn(str(corrupt), " ".join(corrupt_errors))
        self.assertNotIn("private-ca-bundle-marker", " ".join(corrupt_errors))
        self.assertEqual(
            database_tls.check_database_ca(leaf),
            ["database CA bundle must contain a CA certificate"],
        )

    def test_compose_database_mcp_binds_the_required_ca_path(self) -> None:
        compose_text = (ROOT / "deploy/compose.yaml").read_text(encoding="utf-8")
        self.assertEqual(check_runtime_contract(compose_text), [])
        topology = yaml.safe_load(compose_text)

        for replacement in (None, "/run/config/wrong-ca.pem"):
            with self.subTest(replacement=replacement):
                mutated = copy.deepcopy(topology)
                environment = mutated["services"]["database-mcp"]["environment"]
                if replacement is None:
                    environment.pop("PGSSLROOTCERT")
                else:
                    environment["PGSSLROOTCERT"] = replacement
                errors = check_runtime_contract(yaml.safe_dump(mutated))
                self.assertIn(
                    "database-mcp must bind its CA bundle at /run/config/database-ca.pem",
                    errors,
                )

    def test_asyncpg_verify_full_fails_without_an_explicit_root_certificate(self) -> None:
        with self.assertRaises(ClientConfigurationError):
            self.parse_asyncpg(ca_file=None)

    def test_asyncpg_verify_full_loads_the_explicit_ca_and_preserves_read_only_target(self) -> None:
        ca_file = self.write_certificate("parser-ca.pem", ca=True)

        addresses, params = self.parse_asyncpg(ca_file=ca_file)

        self.assertEqual(addresses, [("pg-tls-only.invalid", 5432)])
        self.assertTrue(params.ssl.check_hostname)
        self.assertEqual(params.ssl.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(params.ssl.cert_store_stats()["x509_ca"], 1)
        self.assertEqual(params.target_session_attrs.value, "read-only")


if __name__ == "__main__":
    unittest.main()
