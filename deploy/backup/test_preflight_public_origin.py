"""Public HTTPS origin parsing and port agreement in release preflight."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import preflight

_UNSET = object()
_ORIGIN_ERROR = "EM_PUBLIC_ORIGIN is not an approved concrete HTTPS origin"


class PublicOriginPreflightTests(unittest.TestCase):
    def _check(self, origin: str, public_host: str, https_port: object = _UNSET):
        digest_image = "registry.example.test/service@sha256:" + "a" * 64
        environment = {
            "EM_COMPOSE_PROJECT": "enterprise-mind-prod-public",
            "EM_PUBLIC_ORIGIN": origin,
            "EM_PUBLIC_HOST": public_host,
            "EM_EDGE_IP": "127.0.0.1",
            "EM_EDGE_BIND": "127.0.0.1",
            "EM_SECRET_DIR": "/tmp/em-secrets",
            "EM_CONFIG_DIR": "/tmp/em-config",
            "EM_TLS_CERT_FILE": "/tmp/em-cert.pem",
            "EM_TLS_KEY_FILE": "/tmp/em-key.pem",
            "EM_PYTHON_IMAGE": digest_image,
            "EM_UV_IMAGE": digest_image,
            "EM_NODE_IMAGE": digest_image,
            "EM_NGINX_IMAGE": digest_image,
            "EM_POSTGRES_IMAGE": digest_image,
            "EM_REDIS_IMAGE": digest_image,
            "EM_MINIO_IMAGE": digest_image,
            "EM_MINIO_BUCKET": "test-bucket",
            "EM_BOOTSTRAP_ADMIN_USERNAME": "test-admin",
        }
        if https_port is not _UNSET:
            environment["EM_HTTPS_PORT"] = str(https_port)
        result = preflight.CheckResult()
        with patch.dict(preflight.os.environ, environment, clear=True):
            preflight._check_env(result, static=True)
        return result

    def test_valid_dns_ipv4_ipv6_origins_and_compose_default(self):
        cases = (
            ("HTTPS://APP.Enterprise-Mind.test", "app.enterprise-mind.test", _UNSET),
            (
                "https://app.enterprise-mind.test:443/",
                "app.enterprise-mind.test",
                "443",
            ),
            ("https://app.enterprise-mind.test", "app.enterprise-mind.test", ""),
            ("https://192.0.2.25:8443", "192.0.2.25", "8443"),
            (
                "https://[2001:0db8::5]:443/",
                "2001:db8:0:0:0:0:0:5",
                "443",
            ),
        )
        for origin, public_host, https_port in cases:
            with self.subTest(
                origin=origin, public_host=public_host, https_port=https_port
            ):
                result = self._check(origin, public_host, https_port)
                self.assertEqual(result.errors, [])
                self.assertIn("public host matches HTTPS origin", result.passed)
                self.assertIn("public origin port matches EM_HTTPS_PORT", result.passed)

    def test_malformed_origins_fail_with_fixed_redacted_diagnostic(self):
        cases = (
            " https://app.example.test",
            "https://app.example.test ",
            "https://app.example.test\n",
            "https://app.example.test\t",
            "https://alice:secret@app.example.test",
            "https://@app.example.test",
            "https://app.example.test?",
            "https://app.example.test?token=private-token",
            "https://app.example.test#",
            "https://app.example.test#private-token",
            "https://app.example.test/api",
            "https://app.example.test//",
            "http://app.example.test",
            "https://bad_host.example.test",
            "https://bad..example.test",
            "https://-bad.example.test",
            "https://bad-.example.test",
            "https://" + "a" * 64 + ".example.test",
            "https://app.example.test.",
            "https://999.1.1.1",
            "https://app.example.test:",
            "https://app.example.test:0",
            "https://app.example.test:65536",
            "https://app.example.test:not-a-port",
            "https://app.example.test:0443",
            "https://[2001:db8::1",
            "https://2001:db8::1",
            "https://[2001:db8::1%25eth0]:443",
            "https://[2001:db8::1::2]:443",
            "https://[192.0.2.25]:443",
        )
        for origin in cases:
            with self.subTest(origin=origin):
                result = self._check(origin, "app.example.test")
                self.assertIn(_ORIGIN_ERROR, result.errors)
                diagnostics = " ".join(result.errors)
                self.assertNotIn("secret", diagnostics)
                self.assertNotIn("private-token", diagnostics)
                self.assertNotIn("alice", diagnostics)

    def test_host_and_configured_port_must_match_origin(self):
        host_mismatch = self._check("https://app.example.test", "other.example.test")
        self.assertIn(
            "EM_PUBLIC_HOST does not match EM_PUBLIC_ORIGIN hostname",
            host_mismatch.errors,
        )

        invalid_host = self._check("https://app.example.test", "bad_host.example.test")
        self.assertIn(
            "EM_PUBLIC_HOST is not a valid DNS or IP hostname", invalid_host.errors
        )

        for origin, public_host, port in (
            ("https://app.example.test", "app.example.test", "8443"),
            ("https://app.example.test:8443", "app.example.test", "443"),
        ):
            with self.subTest(origin=origin, configured_port=port):
                result = self._check(origin, public_host, port)
                self.assertIn(
                    "EM_PUBLIC_ORIGIN port does not match EM_HTTPS_PORT", result.errors
                )

    def test_invalid_configured_port_is_rejected_without_echoing_input(self):
        for port in (
            "0",
            "65536",
            "0443",
            "+443",
            " 443",
            "443\n",
            "443\r\n:8443",
            "https",
        ):
            with self.subTest(port=port):
                result = self._check(
                    "https://app.example.test", "app.example.test", port
                )
                self.assertIn("EM_HTTPS_PORT is not a valid TCP port", result.errors)
                self.assertNotIn(port, " ".join(result.errors))

    def test_missing_origin_or_host_is_blocked(self):
        missing_origin = self._check("", "app.example.test")
        self.assertIn(
            "missing environment fields: EM_PUBLIC_ORIGIN", missing_origin.errors
        )

        missing_host = self._check("https://app.example.test", "")
        self.assertIn(
            "EM_PUBLIC_HOST is not a valid DNS or IP hostname", missing_host.errors
        )

        invalid_ipv6_host = self._check("https://[2001:db8::5]", "fe80::5%eth0")
        self.assertIn(
            "EM_PUBLIC_HOST is not a valid DNS or IP hostname", invalid_ipv6_host.errors
        )

        bracketed_ipv6_host = self._check("https://[2001:db8::5]", "[2001:db8::5]")
        self.assertIn(
            "EM_PUBLIC_HOST is not a valid DNS or IP hostname",
            bracketed_ipv6_host.errors,
        )

    def test_url_parser_exceptions_are_redacted_and_do_not_escape(self):
        result = preflight.CheckResult()
        with (
            patch.dict(
                preflight.os.environ,
                {
                    "EM_PUBLIC_ORIGIN": "https://app.example.test",
                    "EM_PUBLIC_HOST": "app.example.test",
                },
                clear=True,
            ),
            patch.object(
                preflight, "urlsplit", side_effect=ValueError("private-parser-detail")
            ),
        ):
            preflight._check_public_origin(result)
        self.assertEqual(result.errors, [_ORIGIN_ERROR])
        self.assertNotIn("private-parser-detail", " ".join(result.errors))


if __name__ == "__main__":
    unittest.main()
