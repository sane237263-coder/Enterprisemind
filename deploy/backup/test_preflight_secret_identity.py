"""Fail-closed namespace inspection and production preflight integration."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import preflight


class SecretIdentityPreflightTests(unittest.TestCase):
    def test_tls_paths_must_remain_outside_repository(self):
        for name in ("EM_TLS_CERT_FILE", "EM_TLS_KEY_FILE"):
            for parent, expected_error in (
                (preflight.REPO_ROOT, True),
                (Path(tempfile.gettempdir()), False),
            ):
                with self.subTest(field=name, inside_repo=expected_error):
                    result = preflight.CheckResult()
                    # No actual key material or file reads are needed for path containment.
                    with (
                        patch.dict(
                            preflight.os.environ, {name: str(parent / "synthetic.pem")}, clear=True
                        ),
                        patch.object(preflight, "check_secret_files", return_value=[]),
                        patch.object(preflight, "check_tls_contract", return_value=[]),
                    ):
                        preflight._check_paths(result, static=False)
                    self.assertEqual(
                        f"{name} must remain outside the repository" in result.errors,
                        expected_error,
                    )

    def test_namespace_inspection_rejects_remapped_daemons(self):
        for options, accepted in (
            (["name=seccomp,profile=builtin", "name=cgroupns"], True),
            (["name=rootless"], False),
            (["name=userns"], False),
            (None, False),
        ):
            with self.subTest(options=options):
                result = preflight.CheckResult()
                completed = SimpleNamespace(returncode=0, stdout=json.dumps(options))
                with patch.object(preflight.subprocess, "run", return_value=completed) as run:
                    preflight._check_docker_secret_namespace(result)
                self.assertEqual(not result.errors, accepted)
                run.assert_called_once_with(
                    ["docker", "info", "--format", "{{json .SecurityOptions}}"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

    def test_inspection_failures_are_blocked_without_raw_output(self):
        marker = "private-daemon-diagnostic"
        for completed, exception in (
            (SimpleNamespace(returncode=1, stdout=marker), None),
            (SimpleNamespace(returncode=0, stdout=marker), None),
            (None, OSError(marker)),
            (None, subprocess.TimeoutExpired(marker, 30)),
        ):
            with self.subTest(exception=type(exception).__name__):
                result = preflight.CheckResult()
                with patch.object(
                    preflight.subprocess, "run", return_value=completed, side_effect=exception
                ):
                    preflight._check_docker_secret_namespace(result)
                self.assertEqual(
                    result.errors, ["Docker identity namespace could not be inspected"]
                )
                self.assertNotIn(marker, str(result.__dict__))

    def test_production_cannot_skip_daemon_inspection(self):
        # Isolate only the CLI gate; these stubs do not prove other release gates.
        for flags, expected_code in (([], 2), (["--static"], 0), (["--compose-check"], 0)):
            with self.subTest(flags=flags):
                output = io.StringIO()
                with (
                    patch.object(sys, "argv", ["preflight.py", *flags]),
                    patch.object(preflight, "_check_env"),
                    patch.object(preflight, "_check_paths", return_value={}),
                    patch.object(preflight, "_check_compose") as compose,
                    redirect_stdout(output),
                ):
                    code = preflight.main()
                payload = json.loads(output.getvalue())
                self.assertEqual(code, expected_code)
                self.assertEqual(payload["status"], "BLOCKED" if expected_code else "PASS")
                self.assertEqual(compose.call_args.kwargs["run_docker"], "--compose-check" in flags)
                if not flags:
                    self.assertEqual(
                        payload["errors"],
                        [
                            "production preflight requires --compose-check "
                            "to verify Docker UID mapping"
                        ],
                    )

    def test_compose_stage_propagates_namespace_failure(self):
        result = preflight.CheckResult()
        with (
            patch.object(preflight, "_check_postgres_image"),
            patch.dict(preflight.os.environ, {"EM_COMPOSE_PROJECT": "enterprise-mind-prod-test"}),
            patch.object(
                preflight.subprocess,
                "run",
                side_effect=[
                    SimpleNamespace(returncode=0, stdout='["name=userns"]'),
                    SimpleNamespace(returncode=0),
                    SimpleNamespace(returncode=0),
                ],
            ),
        ):
            preflight._check_compose(result, run_docker=True)
        self.assertIn("Docker daemon rootless or user namespace mode is unsupported", result.errors)

    def test_office_topology_failure_is_not_hidden_by_base_success(self):
        result = preflight.CheckResult()
        with (
            patch.object(preflight, "_check_postgres_image"),
            patch.object(preflight, "_check_docker_secret_namespace"),
            patch.dict(preflight.os.environ, {"EM_COMPOSE_PROJECT": "enterprise-mind-prod-test"}),
            patch.object(
                preflight.subprocess, "run",
                side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)],
            ) as run,
        ):
            preflight._check_compose(result, run_docker=True)
        self.assertIn("Office-enabled Docker Compose config rendering failed", result.errors)
        self.assertIn(str(preflight.OFFICE_COMPOSE_FILE), run.call_args.args[0])
        self.assertIn("office", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
