"""Safety tests for the P10-10 backup/restore helper."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backup_restore


def container(
    service: str,
    container_port: int,
    host_port: int,
    destination: str,
    volume: str,
    pgdata: str | None = None,
) -> dict[str, object]:
    return {
        "Id": f"{service}-container-id",
        "Config": {
            "Image": f"enterprise-mind-{service}:test",
            "Env": [f"PGDATA={pgdata or destination}"] if service == "postgres" else [],
            "Labels": {
                "com.docker.compose.project": "em-p10-restore-test",
                "com.docker.compose.service": service,
            },
        },
        "State": {"Running": True},
        "NetworkSettings": {
            "Ports": {
                f"{container_port}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(host_port)}]
            },
        },
        "Mounts": [{"Type": "volume", "Name": volume, "Destination": destination}],
    }


class RoleCursor:
    def __init__(self, role_exists: bool = True) -> None:
        self.role_exists = role_exists
        self.query = ""

    def execute(self, query: str, parameters: object) -> None:
        self.query = query

    def fetchone(self) -> tuple[object, ...] | None:
        if "FROM pg_roles" in self.query:
            return (False, False, False, False, False, True) if self.role_exists else None
        return (True,)


class BackupRestoreSafetyTests(unittest.TestCase):
    def test_docker_command_accepts_explicit_wsl_prefix(self) -> None:
        self.assertEqual(
            backup_restore.docker_command("wsl -d Ubuntu -- docker", "ps"),
            ["wsl", "-d", "Ubuntu", "--", "docker", "ps"],
        )

    def test_project_namespace_is_fail_closed(self) -> None:
        with self.assertRaises(backup_restore.BackupError):
            backup_restore.require_project("other-project", kind="source")

    def test_runtime_requires_matching_compose_project_and_new_volumes(self) -> None:
        args = SimpleNamespace(
            target_docker_bin="docker",
            target_project="em-p10-restore-test",
            target_postgres_service="postgres",
            target_redis_service="redis",
            target_minio_service="minio",
            target_pg_port=10101,
            target_redis_port=10102,
            target_minio_endpoint="127.0.0.1:10103",
        )
        inspected = [
            container(
                "postgres",
                5432,
                10101,
                "/var/lib/postgresql",
                "em-p10-restore-test-pg",
                "/var/lib/postgresql/18/docker",
            ),
            container("redis", 6379, 10102, "/data", "em-p10-restore-test-redis"),
            container("minio", 9000, 10103, "/data", "em-p10-restore-test-minio"),
        ]
        with patch(
            "backup_restore.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="pg\nredis\nminio\n", stderr=""),
                SimpleNamespace(returncode=0, stdout=json.dumps(inspected), stderr=""),
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        [{"Labels": {"com.docker.compose.project": "em-p10-restore-test"}}]
                    ),
                    stderr="",
                ),
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        [{"Labels": {"com.docker.compose.project": "em-p10-restore-test"}}]
                    ),
                    stderr="",
                ),
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        [{"Labels": {"com.docker.compose.project": "em-p10-restore-test"}}]
                    ),
                    stderr="",
                ),
            ],
        ):
            result = backup_restore.verify_target_runtime(args)
        self.assertEqual(sorted(result["services"]), ["minio", "postgres", "redis"])

        inspected[0]["Mounts"][0]["Name"] = "shared-unknown-volume"
        with patch(
            "backup_restore.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="pg\nredis\nminio\n", stderr=""),
                SimpleNamespace(returncode=0, stdout=json.dumps(inspected), stderr=""),
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        [{"Labels": {"com.docker.compose.project": "other-project"}}]
                    ),
                    stderr="",
                ),
            ],
        ):
            with self.assertRaises(backup_restore.BackupError):
                backup_restore.verify_target_runtime(args)

    def test_runtime_rejects_public_host_binding(self) -> None:
        args = SimpleNamespace(
            target_docker_bin="docker",
            target_project="em-p10-restore-test",
            target_postgres_service="postgres",
            target_redis_service="redis",
            target_minio_service="minio",
            target_pg_port=10101,
            target_redis_port=10102,
            target_minio_endpoint="127.0.0.1:10103",
        )
        inspected = [
            container(
                "postgres",
                5432,
                10101,
                "/var/lib/postgresql/data",
                "em-p10-restore-test-pg",
                "/var/lib/postgresql/data",
            ),
            container("redis", 6379, 10102, "/data", "em-p10-restore-test-redis"),
            container("minio", 9000, 10103, "/data", "em-p10-restore-test-minio"),
        ]
        inspected[0]["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"
        with patch(
            "backup_restore.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="pg\nredis\nminio\n", stderr=""),
                SimpleNamespace(returncode=0, stdout=json.dumps(inspected), stderr=""),
            ],
        ):
            with self.assertRaises(backup_restore.BackupError):
                backup_restore.verify_target_runtime(args)

    def test_runtime_supports_pg17_data_mount(self) -> None:
        args = SimpleNamespace(
            target_docker_bin="docker",
            target_project="em-p10-restore-test",
            target_postgres_service="postgres",
            target_redis_service="redis",
            target_minio_service="minio",
            target_pg_port=10101,
            target_redis_port=10102,
            target_minio_endpoint="127.0.0.1:10103",
        )
        inspected = [
            container(
                "postgres",
                5432,
                10101,
                "/var/lib/postgresql/data",
                "em-p10-restore-test-pg",
                "/var/lib/postgresql/data",
            ),
            container("redis", 6379, 10102, "/data", "em-p10-restore-test-redis"),
            container("minio", 9000, 10103, "/data", "em-p10-restore-test-minio"),
        ]
        with patch(
            "backup_restore.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="pg\nredis\nminio\n", stderr=""),
                SimpleNamespace(returncode=0, stdout=json.dumps(inspected), stderr=""),
                *[
                    SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(
                            [{"Labels": {"com.docker.compose.project": "em-p10-restore-test"}}]
                        ),
                        stderr="",
                    )
                    for _ in range(3)
                ],
            ],
        ):
            result = backup_restore.verify_target_runtime(args)
        self.assertEqual(
            result["services"]["postgres"]["volume_destinations"],
            ["/var/lib/postgresql/data"],
        )
        self.assertEqual(result["services"]["postgres"]["pgdata"], "/var/lib/postgresql/data")

    def test_pgdata_most_specific_bind_mount_is_rejected(self) -> None:
        args = SimpleNamespace(
            target_docker_bin="docker",
            target_project="em-p10-restore-test",
            target_postgres_service="postgres",
            target_redis_service="redis",
            target_minio_service="minio",
            target_pg_port=10101,
            target_redis_port=10102,
            target_minio_endpoint="127.0.0.1:10103",
        )
        inspected = [
            container(
                "postgres",
                5432,
                10101,
                "/var/lib/postgresql",
                "em-p10-restore-test-pg",
                "/var/lib/postgresql/18/docker",
            ),
            container("redis", 6379, 10102, "/data", "em-p10-restore-test-redis"),
            container("minio", 9000, 10103, "/data", "em-p10-restore-test-minio"),
        ]
        inspected[0]["Mounts"].append(
            {"Type": "bind", "Name": "", "Destination": "/var/lib/postgresql/18/docker"}
        )
        with patch(
            "backup_restore.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="pg\nredis\nminio\n", stderr=""),
                SimpleNamespace(returncode=0, stdout=json.dumps(inspected), stderr=""),
            ],
        ) as run:
            with self.assertRaises(backup_restore.BackupError):
                backup_restore.verify_target_runtime(args)
        self.assertEqual(run.call_count, 2)

    def test_restore_requires_preprovisioned_role_and_grant(self) -> None:
        args = SimpleNamespace(target_pg_user="target_owner")
        manifest = {
            "postgres": {
                "username": "source_owner",
                "roles_and_grants": {
                    "path": "postgres/roles-and-grants.json",
                    "roles_without_passwords": [
                        {
                            "name": "source_owner",
                            "superuser": False,
                            "createdb": False,
                            "createrole": False,
                            "replication": False,
                            "bypassrls": False,
                            "inherit": True,
                        }
                    ],
                    "table_grants": [
                        {
                            "grantee": "source_owner",
                            "schema": "iam",
                            "table": "users",
                            "privilege": "SELECT",
                        }
                    ],
                },
            }
        }
        manifest["postgres"]["roles_and_grants"]["table_grants"].extend(
            [
                {
                    "grantee": "source_owner",
                    "schema": "iam",
                    "table": "users",
                    "privilege": "TRUNCATE",
                },
                {
                    "grantee": "source_owner",
                    "schema": "iam",
                    "table": "users",
                    "privilege": "MAINTAIN",
                },
            ]
        )
        result = backup_restore.verify_restored_roles(RoleCursor(), args, manifest)
        self.assertEqual(result, {"roles_checked": 1, "table_grants_checked": 3})
        with self.assertRaises(backup_restore.BackupError):
            backup_restore.verify_restored_roles(RoleCursor(role_exists=False), args, manifest)

    def test_backup_artifact_validation_blocks_tampered_files_and_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dump = root / "postgres" / "enterprise_mind.dump"
            dump.parent.mkdir()
            dump.write_bytes(b"safe dump")
            roles = root / "postgres" / "roles-and-grants.json"
            roles.write_text(
                json.dumps({"roles_without_passwords": [], "table_grants": []}),
                encoding="utf-8",
            )
            manifest = {
                "kind": "enterprise-mind-p10-10-backup",
                "postgres": {
                    "dump": {
                        "path": "postgres/enterprise_mind.dump",
                        "size": dump.stat().st_size,
                        "sha256": backup_restore.sha256_file(dump),
                        "format": "custom",
                    },
                    "roles_and_grants": {
                        "path": "postgres/roles-and-grants.json",
                        "size": roles.stat().st_size,
                        "sha256": backup_restore.sha256_file(roles),
                        "roles_without_passwords": [],
                        "table_grants": [],
                    },
                },
                "minio": {"objects": []},
                "release_materials": [],
            }
            backup_restore.validate_backup_artifacts(root, manifest)

            dump.write_bytes(b"changed")
            with self.assertRaises(backup_restore.BackupError):
                backup_restore.validate_backup_artifacts(root, manifest)
            with self.assertRaises(backup_restore.BackupError):
                backup_restore.validate_backup_file(root, "../outside", 0, "0" * 64, label="test")

    def test_restore_refuses_nonempty_redis_before_starting_pg_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "backup"
            (backup / "postgres").mkdir(parents=True)
            dump = backup / "postgres" / "enterprise_mind.dump"
            dump.write_bytes(b"safe dump")
            roles_path = backup / "postgres" / "roles-and-grants.json"
            role_file = {"roles_without_passwords": [], "table_grants": []}
            roles_path.write_text(json.dumps(role_file), encoding="utf-8")
            manifest = {
                "kind": "enterprise-mind-p10-10-backup",
                "completed_at": "2026-09-26T00:00:00+00:00",
                "postgres": {
                    "username": "em_restore_owner",
                    "dump": {
                        "path": "postgres/enterprise_mind.dump",
                        "size": dump.stat().st_size,
                        "sha256": backup_restore.sha256_file(dump),
                        "format": "custom",
                    },
                    "roles_and_grants": {
                        "path": "postgres/roles-and-grants.json",
                        "size": roles_path.stat().st_size,
                        "sha256": backup_restore.sha256_file(roles_path),
                        **role_file,
                    },
                },
                "minio": {"objects": []},
                "release_materials": [],
            }
            (backup / "backup-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            evidence = root / "restore-evidence.json"
            args = SimpleNamespace(
                dry_run=False,
                allow_restore=True,
                backup=backup,
                evidence=evidence,
                pg_restore_bin="pg_restore",
            )
            with (
                patch("backup_restore.target_empty_checks", return_value={}),
                patch("backup_restore.verify_target_runtime", return_value={}),
                patch("backup_restore.verify_empty_postgres_target", return_value={}),
                patch("backup_restore.verify_empty_minio_target", return_value={}),
                patch(
                    "backup_restore.verify_empty_redis_target",
                    side_effect=backup_restore.BackupError("target Redis is not empty"),
                ),
                patch("backup_restore.run_checked") as run_checked,
            ):
                self.assertEqual(backup_restore.restore(args), 2)
            self.assertEqual(run_checked.call_count, 1)
            self.assertIn("--list", run_checked.call_args.args[0])
            self.assertNotIn("--exit-on-error", run_checked.call_args.args[0])
            result = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "BLOCKED")
            self.assertFalse(result["writes_started"])

    def test_restore_function_itself_requires_write_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "restore-evidence.json"
            args = SimpleNamespace(
                dry_run=False,
                allow_restore=False,
                evidence=evidence,
            )
            self.assertEqual(backup_restore.restore(args), 2)
            self.assertEqual(json.loads(evidence.read_text(encoding="utf-8"))["status"], "BLOCKED")

    def test_empty_minio_preflight_rejects_existing_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret_dir = Path(directory)
            (secret_dir / "minio_app_access").write_text("access", encoding="utf-8")
            (secret_dir / "minio_app_secret").write_text("secret", encoding="utf-8")
            args = SimpleNamespace(
                target_minio_endpoint="127.0.0.1:10103",
                target_secret_dir=secret_dir,
                target_minio_bucket="em-restore-source",
            )
            with patch(
                "backup_restore.Minio",
                return_value=SimpleNamespace(bucket_exists=lambda _: True),
            ):
                with self.assertRaises(backup_restore.BackupError):
                    backup_restore.verify_empty_minio_target(args)

    def test_empty_redis_preflight_rejects_existing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "redis_password"
            secret.write_text("password", encoding="utf-8")
            args = SimpleNamespace(
                target_redis_host="127.0.0.1",
                target_redis_port=10102,
                target_redis_user="enterprise_mind_app",
                target_secret_dir=Path(directory),
            )
            client = SimpleNamespace(ping=lambda: True, dbsize=lambda: 1)
            with patch("backup_restore.redis.Redis", return_value=client):
                with self.assertRaises(backup_restore.BackupError):
                    backup_restore.verify_empty_redis_target(args)

    def test_same_bucket_name_is_allowed_at_a_distinct_restore_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "target-identity.json"
            marker.write_text(
                json.dumps(
                    {
                        "project": "em-p10-restore-test",
                        "identity_verified": True,
                        "target_data_is_new": True,
                        "resource_scope": "EnterpriseMind-only",
                    }
                ),
                encoding="utf-8",
            )
            secret_dir = root / "secrets"
            secret_dir.mkdir()
            for name in (
                "postgres_owner_password",
                "minio_app_access",
                "minio_app_secret",
                "redis_app_password",
                "token_signing_key",
            ):
                (secret_dir / name).write_text(f"new-{name}", encoding="utf-8")
            manifest = {
                "source": {"project": "em-p10-source"},
                "postgres": {"port": 10001},
                "redis": {"endpoint": "127.0.0.1:10002"},
                "minio": {"endpoint": "127.0.0.1:10003", "bucket": "same-bucket"},
                "secrets": {"fingerprints_only": {"token_signing_key": "0" * 64}},
            }
            args = SimpleNamespace(
                target_project="em-p10-restore-test",
                target_identity=marker,
                target_pg_host="127.0.0.1",
                target_pg_port=10101,
                target_redis_host="127.0.0.1",
                target_redis_port=10102,
                target_minio_endpoint="127.0.0.1:10103",
                target_minio_bucket="same-bucket",
                target_secret_dir=secret_dir,
                dry_run=True,
            )
            result = backup_restore.target_empty_checks(args, manifest)
            self.assertIsNone(result["target_data_is_new"])
            self.assertTrue(result["target_identity_marker_attests_new_data"])


if __name__ == "__main__":
    unittest.main()
