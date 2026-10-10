"""Offline startup regressions; no containers, network, or real credentials."""

# This standalone test suite also runs without installing the local package.
# ruff: noqa: E402

from __future__ import annotations

import copy
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))
sys.path.insert(0, str(ROOT / "backend/src"))

from enterprise_mind.infrastructure.settings import (
    APISettings,
    BootstrapSettings,
    OfficeConverterConfig,
    load_application_config,
)
from pydantic import ValidationError
from runtime_contract_check import check_application_contract, check_runtime_contract


class RuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.compose = (ROOT / "deploy/compose.yaml").read_text(encoding="utf-8")
        self.topology = yaml.safe_load(self.compose)
        # YAML anchors share objects. Mutating a service must affect only that service.
        self.services = self.topology["services"]
        for name, value in list(self.services.items()):
            self.services[name] = copy.deepcopy(value)

    def errors(self):
        return check_runtime_contract(yaml.safe_dump(self.topology))

    def test_current_release_topology(self):
        self.assertEqual(check_runtime_contract(self.compose), [])

    def test_application_contract_agrees_with_runtime_loading(self):
        config_path = ROOT / "deploy/config/application.production.yaml"
        self.assertEqual(load_application_config(config_path).api.workers, 1)
        self.assertEqual(check_application_contract(config_path.read_text(encoding="utf-8")), [])
        for value in ("api: {workers: 1}", "api: &single {workers: 1}\n"):
            with self.subTest(config=value):
                self.assertEqual(check_application_contract(value), [])

    def test_application_contract_checks_effective_count_and_defaults(self):
        for value in (
            "api:\n  workers: 1\napi:\n  workers: 2\n",
            "api: {workers: 2}",
            "{}",
            "api: {}",
        ):
            with self.subTest(config=value):
                self.assertEqual(
                    check_application_contract(value),
                    [
                        "application config must pin api.workers to 1 for "
                        "process-local page/export context"
                    ],
                )

    def test_application_contract_allows_only_internal_office_converter_origin(self):
        for origin in (
            "http://office-converter:8080",
            "http://office-converter:8080/",
        ):
            with self.subTest(origin=origin):
                application = yaml.safe_dump(
                    {
                        "api": {"workers": 1},
                        "office_converter": {"base_url": origin},
                    }
                )
                self.assertEqual(check_application_contract(application), [])

        application = yaml.safe_dump({"api": {"workers": 1}, "office_converter": None})
        self.assertEqual(check_application_contract(application), [])

    def test_application_contract_rejects_non_internal_office_origins_without_echo(self):
        internal_error = "office converter must use the internal service origin"
        schema_error = "application config does not match the runtime schema"
        rejected_origins = (
            ("http://outside.invalid:8080", internal_error),
            ("http://192.0.2.17:8080", internal_error),
            ("https://office-converter:8080", internal_error),
            ("http://office-converter:8081", internal_error),
            ("http://office-converter:99999", internal_error),
            ("http://office-converter:not-a-port", internal_error),
            ("http://office-converter:8080/other", schema_error),
            (
                "http://office-converter:8080?token=private-query",
                schema_error,
            ),
            (
                "http://office-converter:8080#private-fragment",
                schema_error,
            ),
            ("http://office-converter:8080?", internal_error),
            ("http://office-converter:8080#", internal_error),
            ("http://user:private-password@office-converter:8080", schema_error),
            ("http://@office-converter:8080", internal_error),
            ("http://office-converter:8080\n", internal_error),
        )
        for origin, expected_error in rejected_origins:
            with self.subTest(origin=origin):
                application = yaml.safe_dump(
                    {
                        "api": {"workers": 1},
                        "office_converter": {"base_url": origin},
                    }
                )
                errors = check_application_contract(application)
                self.assertEqual(errors, [expected_error])
                self.assertNotIn(origin, " ".join(errors))
                self.assertNotIn("private-password", " ".join(errors))
                self.assertNotIn("private-query", " ".join(errors))
                self.assertNotIn("private-fragment", " ".join(errors))

    def test_application_contract_keeps_worker_count_error_precedence(self):
        application = yaml.safe_dump(
            {
                "api": {"workers": 2},
                "office_converter": {"base_url": "http://outside.invalid:8080"},
            }
        )
        self.assertEqual(
            check_application_contract(application),
            ["application config must pin api.workers to 1 for process-local page/export context"],
        )

    def test_development_localhost_converter_origin_remains_valid_in_generic_schema(self):
        config = OfficeConverterConfig(base_url="http://127.0.0.1:18808")
        self.assertEqual(config.base_url, "http://127.0.0.1:18808")

        application = yaml.safe_dump(
            {
                "api": {"workers": 1},
                "office_converter": {"base_url": config.base_url},
            }
        )
        self.assertEqual(
            check_application_contract(application),
            ["office converter must use the internal service origin"],
        )

    def test_application_contract_rejects_runtime_invalid_values_without_echo(self):
        for value in (
            "api: {workers: '1'}",
            "api: {workers: true}",
            "api: {workers: 1}\nworker: {lease_seconds: 30, lease_renew_interval_seconds: 30}",
            "api: {workers: 1}\nprivate-field: synthetic-private-value",
            "[synthetic-private-value]",
            "{synthetic-private-value",
            "",
        ):
            with self.subTest(config=value):
                self.assertEqual(
                    check_application_contract(value),
                    ["application config does not match the runtime schema"],
                )

    def test_bootstrap_superuser_cannot_be_application_owner(self):
        for name, variable in (("postgres", "POSTGRES_USER"), ("db-provision", "EM_POSTGRES_USER")):
            self.services[name]["environment"][variable] = "enterprise_mind_owner"
        self.assertIn(
            "PostgreSQL bootstrap login must differ from all application roles", self.errors()
        )

    def test_mismatched_provision_login(self):
        self.services["db-provision"]["environment"]["EM_POSTGRES_USER"] = "wrong-login"
        self.assertIn("db-provision must use the PostgreSQL bootstrap login", self.errors())

    def test_mounted_secret_is_not_enough_without_api_setting(self):
        del self.services["api"]["environment"]["EM_TOKEN_SIGNING_KEY_FILE"]
        self.assertIn(
            "api: EM_TOKEN_SIGNING_KEY_FILE must reference a mounted Secret", self.errors()
        )

    def test_initial_administrator_requires_username_and_secret(self):
        for variable in ("EM_BOOTSTRAP_ADMIN_USERNAME", "EM_BOOTSTRAP_ADMIN_PASSWORD_FILE"):
            del self.services["migrate"]["environment"][variable]
        errors = self.errors()
        self.assertIn("migrate must bind the initial administrator username", errors)
        self.assertIn(
            "migrate: EM_BOOTSTRAP_ADMIN_PASSWORD_FILE must reference a mounted Secret", errors
        )

    def test_worker_needs_a_non_internal_network_for_model_calls(self):
        self.services["worker"]["networks"] = ["app_net", "mcp_net", "conversion_net"]
        self.assertIn("worker: all networks isolate external model endpoints", self.errors())

    def test_converter_token_must_be_mounted_by_worker(self):
        self.services["worker"]["secrets"].remove("converter_token")
        self.assertIn(
            "worker: EM_OFFICE_CONVERTER_TOKEN_FILE must reference a mounted Secret", self.errors()
        )

    def test_checkpoint_init_cannot_trigger_a_second_migration(self):
        self.services["checkpoint-init"]["depends_on"]["migrate"] = {
            "condition": "service_completed_successfully"
        }
        self.assertIn("checkpoint-init must not automatically rerun migrations", self.errors())

    def test_root_owned_cache_is_not_writable_by_non_root_edge(self):
        self.services["edge"]["tmpfs"] = ["/tmp:mode=1777", "/var/cache/nginx:size=64m,mode=0755"]
        self.assertIn(
            "edge cache tmpfs must be owned and writable by its runtime user", self.errors()
        )

    def test_bootstrap_settings_do_not_leak_to_runtime(self):
        self.services["api"]["environment"]["EM_BOOTSTRAP_ADMIN_USERNAME"] = "bootstrap-user"
        self.assertIn(
            "api: runtime must not receive administrator bootstrap settings", self.errors()
        )

    def test_invalid_shapes_are_rejected_without_echoing_input(self):
        for value in ("[private-value]", "services: [private-value]", "{unclosed"):
            with self.subTest(value=value):
                errors = check_runtime_contract(value)
                self.assertTrue(errors)
                self.assertNotIn("private-value", " ".join(errors))

    def test_actual_api_settings_accept_release_environment_and_reject_old_missing_key(self):
        environment = dict(self.services["api"]["environment"])
        environment["EM_PUBLIC_ORIGIN"] = "https://enterprise-mind.test"
        with patch.dict(os.environ, environment, clear=True):
            settings = APISettings()
            self.assertEqual(settings.postgres_user, "enterprise_mind_app")
            self.assertEqual(
                settings.token_signing_key_file, Path("/run/secrets/token_signing_key")
            )
            del os.environ["EM_TOKEN_SIGNING_KEY_FILE"]
            with self.assertRaises(ValidationError) as caught:
                APISettings()
            self.assertEqual(caught.exception.errors()[0]["loc"], ("token_signing_key_file",))

    def test_actual_bootstrap_settings_bind_only_to_migration_service(self):
        environment = dict(self.services["migrate"]["environment"])
        environment["EM_BOOTSTRAP_ADMIN_USERNAME"] = "Synthetic Bootstrap Admin"
        with patch.dict(os.environ, environment, clear=True):
            settings = BootstrapSettings()
            self.assertEqual(settings.bootstrap_admin_username, "Synthetic Bootstrap Admin")
            self.assertEqual(
                settings.bootstrap_admin_password_file,
                Path("/run/secrets/bootstrap_admin_password"),
            )


if __name__ == "__main__":
    unittest.main()
