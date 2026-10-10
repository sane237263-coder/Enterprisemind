"""Offline TLS preflight regressions using generated test-only material."""

# ruff: noqa: E402

from __future__ import annotations

import ipaddress
import os
import stat
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))

import tls_contract_check as tls_checker


class TLSContractCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cert_key = ec.generate_private_key(ec.SECP256R1())

    def write_pair(
        self,
        *,
        san_names: tuple[x509.GeneralName, ...] | None = None,
        starts_in: timedelta = timedelta(days=-1),
        expires_in: timedelta = timedelta(days=30),
        certificate_key=None,
        key_file_key=None,
        encrypted: bool = False,
    ) -> tuple[Path, Path]:
        certificate_key = certificate_key or self.cert_key
        key_file_key = key_file_key or certificate_key
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "offline-test")])
        now = datetime.now(UTC).replace(tzinfo=None)
        if san_names is None:
            san_names = (x509.DNSName("enterprise-mind.test"),)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(certificate_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now + starts_in)
            .not_valid_after(now + expires_in)
            .add_extension(x509.SubjectAlternativeName(san_names), critical=False)
            .sign(certificate_key, hashes.SHA256())
        )
        cert_path = self.root / "leaf.pem"
        key_path = self.root / "leaf-key.pem"
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        encryption = (
            serialization.BestAvailableEncryption(b"key-encryption-fixture")
            if encrypted
            else serialization.NoEncryption()
        )
        key_path.write_bytes(
            key_file_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                encryption,
            )
        )
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return cert_path, key_path

    def test_valid_pair_matches_dns_san_case_insensitively(self):
        cert_path, key_path = self.write_pair()

        errors = tls_checker.check_tls_contract(cert_path, key_path, "ENTERPRISE-MIND.TEST")

        self.assertEqual(errors, [])

    def test_ip_san_uses_ip_address_comparison(self):
        cert_path, key_path = self.write_pair(
            san_names=(x509.IPAddress(ipaddress.ip_address("2001:db8::1")),)
        )

        errors = tls_checker.check_tls_contract(
            cert_path, key_path, "2001:0db8:0000:0000:0000:0000:0000:0001"
        )

        self.assertEqual(errors, [])

    def test_wrong_host_does_not_match_certificate_san(self):
        cert_path, key_path = self.write_pair()

        errors = tls_checker.check_tls_contract(cert_path, key_path, "other.enterprise-mind.test")

        self.assertIn("tls_cert_san_mismatch", errors)

    def test_wildcard_san_does_not_replace_exact_san_match(self):
        cert_path, key_path = self.write_pair(san_names=(x509.DNSName("*.enterprise-mind.test"),))

        errors = tls_checker.check_tls_contract(cert_path, key_path, "app.enterprise-mind.test")

        self.assertIn("tls_cert_san_mismatch", errors)

    def test_expired_certificate_is_rejected(self):
        cert_path, key_path = self.write_pair(
            starts_in=timedelta(days=-3), expires_in=timedelta(days=-1)
        )

        errors = tls_checker.check_tls_contract(cert_path, key_path, "enterprise-mind.test")

        self.assertIn("tls_cert_expired", errors)

    def test_not_yet_valid_certificate_is_rejected(self):
        cert_path, key_path = self.write_pair(
            starts_in=timedelta(days=1), expires_in=timedelta(days=2)
        )

        errors = tls_checker.check_tls_contract(cert_path, key_path, "enterprise-mind.test")

        self.assertIn("tls_cert_not_yet_valid", errors)

    def test_mismatched_private_key_is_rejected(self):
        unrelated_key = ec.generate_private_key(ec.SECP256R1())
        cert_path, key_path = self.write_pair(key_file_key=unrelated_key)

        errors = tls_checker.check_tls_contract(cert_path, key_path, "enterprise-mind.test")

        self.assertIn("tls_cert_key_pair_invalid", errors)

    def test_malformed_certificate_returns_redacted_labels(self):
        cert_path, key_path = self.write_pair()
        cert_path.write_bytes(b"not-a-certificate")

        errors = tls_checker.check_tls_contract(cert_path, key_path, "enterprise-mind.test")

        self.assertIn("tls_cert_decode_failed", errors)
        self.assertIn("tls_cert_key_pair_invalid", errors)
        self.assertNotIn(str(cert_path), " ".join(errors))
        self.assertNotIn("not-a-certificate", " ".join(errors))

    def test_encrypted_private_key_fails_without_prompt_or_secret_echo(self):
        cert_path, key_path = self.write_pair(encrypted=True)

        errors = tls_checker.check_tls_contract(cert_path, key_path, "enterprise-mind.test")

        self.assertIn("tls_cert_key_pair_invalid", errors)
        self.assertNotIn("key-encryption-fixture", " ".join(errors))

    def test_missing_and_non_regular_files_have_fixed_labels(self):
        cert_path, key_path = self.write_pair()

        missing_errors = tls_checker.check_tls_contract(
            self.root / "missing.pem", key_path, "enterprise-mind.test"
        )
        type_errors = tls_checker.check_tls_contract(self.root, key_path, "enterprise-mind.test")

        self.assertIn("tls_cert_missing", missing_errors)
        self.assertIn("tls_cert_not_regular_file", type_errors)
        self.assertNotIn(str(cert_path), " ".join(missing_errors + type_errors))

    def test_linux_key_mode_requires_owner_read_and_no_group_or_other_access(self):
        cert_path, key_path = self.write_pair()
        original_stat = Path.stat

        for mode in (stat.S_IFREG | 0o640, stat.S_IFREG | 0o200):
            with self.subTest(mode=oct(mode)):

                def simulated_stat(path, *args, _mode=mode, **kwargs):
                    if path == key_path:
                        return SimpleNamespace(st_mode=_mode)
                    return original_stat(path, *args, **kwargs)

                with (
                    patch.object(tls_checker.sys, "platform", "linux"),
                    patch.object(Path, "stat", simulated_stat),
                ):
                    errors = tls_checker.check_tls_contract(
                        cert_path, key_path, "enterprise-mind.test"
                    )
                self.assertIn("tls_key_permissions_unsafe", errors)


if __name__ == "__main__":
    unittest.main()
