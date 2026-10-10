"""Offline Secret topology and metadata checks; no Docker or Secret contents."""

from __future__ import annotations

import copy
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))

import secret_contract_check as checker  # noqa: E402


class SecretTopologyTests(unittest.TestCase):
    def setUp(self):
        self.compose = (ROOT / "deploy/compose.yaml").read_text(encoding="utf-8")
        self.topology = yaml.safe_load(self.compose)
        self.services = {
            name: copy.deepcopy(service) for name, service in self.topology["services"].items()
        }
        self.topology["services"] = self.services

    def errors(self):
        return checker.check_secret_topology(yaml.safe_dump(self.topology))

    def test_current_topology(self):
        self.assertEqual(checker.check_secret_topology(self.compose), [])

    def test_postgres_empty_command_cannot_disable_image_default(self):
        for command in ([], "", " ", {}, False):
            with self.subTest(command=command):
                self.services["postgres"]["command"] = command
                self.assertIn("postgres command must retain the image default", self.errors())

    def test_custom_service_must_use_fixed_uid(self):
        self.services["api"]["user"] = "1000:1000"
        self.assertIn("api must run as 65532:65532", self.errors())

    def test_data_services_reject_even_empty_entrypoint_overrides(self):
        for service_name in ("postgres", "redis", "minio"):
            for override in ([], ""):
                with self.subTest(service=service_name, override=override):
                    self.services[service_name]["entrypoint"] = override
                    self.assertIn(
                        f"{service_name} cannot replace its image entrypoint", self.errors()
                    )
                    del self.services[service_name]["entrypoint"]

    def test_data_commands_must_retain_image_commands(self):
        for service_name, diagnostic in (
            ("postgres", "postgres command must retain the image default"),
            ("redis", "redis command must retain its image command"),
            ("minio", "minio command must retain its image command"),
        ):
            with self.subTest(service=service_name):
                self.services[service_name]["command"] = ["sh"]
                self.assertIn(diagnostic, self.errors())

    def test_root_data_services_cannot_drop_required_read_capability(self):
        for service_name in ("postgres", "redis", "minio"):
            for cap in ("ALL", "DAC_OVERRIDE"):
                with self.subTest(service=service_name, cap=cap):
                    self.services[service_name]["cap_drop"] = [cap]
                    self.assertIn(
                        f"{service_name} root Secret reader capabilities are required",
                        self.errors(),
                    )

    def test_user_namespace_override_is_rejected(self):
        self.services["api"]["userns_mode"] = "host"
        self.assertIn("api cannot override the user namespace", self.errors())

    def test_unknown_secret_mount_is_rejected_without_echoing_input(self):
        marker = "private-secret-name"
        self.services["api"]["secrets"].append(marker)
        errors = " ".join(self.errors())
        self.assertIn("api has an unsupported Secret mount", errors)
        self.assertNotIn(marker, errors)

    def test_mount_target_must_preserve_run_secrets_name(self):
        mounts = self.services["api"]["secrets"]
        mounts[0] = {"source": mounts[0], "target": "/run/secrets/alias"}
        self.assertTrue(any("target must preserve" in error for error in self.errors()))

    def test_compose_uid_gid_mode_remapping_is_rejected(self):
        mounts = self.services["api"]["secrets"]
        mounts[0] = {"source": mounts[0], "uid": "65532", "gid": "65532", "mode": 0o400}
        self.assertTrue(any("cannot remap ownership or mode" in error for error in self.errors()))

    def test_canonical_long_source_and_target_are_accepted(self):
        mounts = self.services["api"]["secrets"]
        source = mounts[0]
        mounts[0] = {"source": source, "target": f"/run/secrets/{source}"}
        self.assertEqual(self.errors(), [])

    def test_secret_source_path_is_canonical_and_not_echoed(self):
        marker = "/private/path/secret"
        self.topology["secrets"]["postgres_app_password"]["file"] = marker
        errors = " ".join(self.errors())
        self.assertIn("postgres_app_password Secret source must use its canonical path", errors)
        self.assertNotIn(marker, errors)


class SecretFileMetadataTests(unittest.TestCase):
    def make_files(self, root: Path):
        secret_dir = root / "secrets"
        secret_dir.mkdir()
        for filename in checker.SECRET_FILE_UIDS:
            (secret_dir / filename).write_bytes(b"test-only")
        cert, key = root / "cert.pem", root / "key.pem"
        cert.write_bytes(b"test-only")
        key.write_bytes(b"test-only")
        return secret_dir, cert, key

    def run_with_metadata(self, secret_dir: Path, cert: Path, key: Path, overrides=None):
        overrides = overrides or {}
        original_lstat = Path.lstat
        entries = {secret_dir: (0, stat.S_IFDIR | 0o700)}
        entries.update(
            {
                secret_dir / name: (uid, stat.S_IFREG | 0o400)
                for name, uid in checker.SECRET_FILE_UIDS.items()
                if (secret_dir / name).exists()
            }
        )
        entries[cert] = (101, stat.S_IFREG | 0o644)
        entries[key] = (101, stat.S_IFREG | 0o600)
        entries.update(overrides)

        def fake_lstat(path):
            if path in entries:
                uid, mode = entries[path]
                return SimpleNamespace(st_uid=uid, st_mode=mode)
            return original_lstat(path)

        with (
            patch.object(checker.sys, "platform", "linux"),
            patch.object(Path, "lstat", fake_lstat),
        ):
            return checker.check_secret_files(secret_dir, cert, key)

    def test_valid_linux_metadata_accepts_canonical_uids_and_tls_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret_dir, cert, key = self.make_files(root)
            self.assertEqual(self.run_with_metadata(secret_dir, cert, key), [])

    def test_wrong_owner_and_secret_mode_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret_dir, cert, key = self.make_files(root)
            override = {secret_dir / "redis.acl": (65532, stat.S_IFREG | 0o640)}
            errors = self.run_with_metadata(secret_dir, cert, key, override)
            self.assertIn("redis.acl has the wrong owner", errors)
            self.assertIn("redis.acl must use mode 0400 or 0600", errors)

    def test_missing_secret_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret_dir, cert, key = self.make_files(root)
            (secret_dir / "llm_api_key").unlink()
            errors = self.run_with_metadata(secret_dir, cert, key)
            self.assertIn("llm_api_key file is missing or unreadable as metadata", errors)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret_dir, cert, key = self.make_files(root)
            symlink_metadata = {secret_dir / "redis.acl": (999, stat.S_IFLNK | 0o777)}
            errors = self.run_with_metadata(secret_dir, cert, key, symlink_metadata)
            self.assertIn("Secret directory cannot contain symbolic links", errors)
            self.assertIn("redis.acl must be a regular non-symlink file", errors)

    def test_secret_directory_must_be_root_owned_and_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret_dir, cert, key = self.make_files(root)
            overrides = {secret_dir: (1000, stat.S_IFDIR | 0o755)}
            errors = self.run_with_metadata(secret_dir, cert, key, overrides)
            self.assertIn("Secret directory must be root-owned mode 0700", errors)

    def test_tls_key_and_certificate_policies_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret_dir, cert, key = self.make_files(root)
            overrides = {
                cert: (101, stat.S_IFREG | 0o0664),
                key: (101, stat.S_IFREG | 0o0644),
            }
            errors = self.run_with_metadata(secret_dir, cert, key, overrides)
            self.assertIn(
                "TLS certificate must be owner-readable without group or other writes", errors
            )
            self.assertIn("TLS private key must use mode 0400 or 0600", errors)

    def test_tls_files_must_be_owned_by_edge_uid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret_dir, cert, key = self.make_files(root)
            errors = self.run_with_metadata(
                secret_dir, cert, key, {key: (999, stat.S_IFREG | 0o600)}
            )
            self.assertIn("TLS private key has the wrong owner", errors)

    def test_non_linux_metadata_is_not_claimed_as_verified(self):
        with patch.object(checker.sys, "platform", "win32"):
            self.assertEqual(
                checker.check_secret_files(Path("secrets"), Path("cert"), Path("key")),
                ["Secret file ownership checks require Linux metadata"],
            )


class DaemonSecurityOptionsTests(unittest.TestCase):
    def test_supported_host_security_options(self):
        self.assertEqual(
            checker.check_daemon_security_options(
                ["name=seccomp,profile=builtin", "name=cgroupns"]
            ),
            [],
        )

    def test_rootless_and_userns_are_rejected(self):
        for option in ("name=rootless", "name=userns", "name=userns,foo=bar"):
            with self.subTest(option=option):
                self.assertTrue(checker.check_daemon_security_options([option]))

    def test_invalid_security_options_shape_is_rejected(self):
        for options in (None, "name=rootless", ["name=seccomp", 7]):
            with self.subTest(options=options):
                self.assertEqual(
                    checker.check_daemon_security_options(options),
                    ["Docker daemon security options are invalid"],
                )


if __name__ == "__main__":
    unittest.main()
