"""Offline checks for the fixed Compose Secret ownership contract."""

from __future__ import annotations

import re
import stat
import sys
from pathlib import Path

import yaml

SECRET_FILE_UIDS = {
    "postgres_owner_password": 65532,
    "postgres_migrator_password": 65532,
    "postgres_app_password": 65532,
    "postgres_backup_password": 65532,
    "postgres_checkpoint_password": 65532,
    "bootstrap_admin_password": 65532,
    "redis.acl": 999,
    "redis_app_password": 65532,
    "minio_root_access": 65532,
    "minio_root_secret": 65532,
    "minio_app_access": 65532,
    "minio_app_secret": 65532,
    "token_signing_key": 65532,
    "database_mcp_token": 65532,
    "database_readonly_password": 65532,
    "llm_api_key": 65532,
    "embedding_api_key": 65532,
    "rerank_api_key": 65532,
    "ocr_api_key": 65532,
    "ocr_secret_id": 65532,
    "ocr_secret_key": 65532,
    "vision_api_key": 65532,
    "converter_token": 65532,
}

_SECRET_IDS = {
    ("redis_acl" if filename == "redis.acl" else filename): filename
    for filename in SECRET_FILE_UIDS
}
_TLS_UIDS = {"tls_certificate": 101, "tls_private_key": 101}
_SERVICE_USERS = {
    **{
        name: "65532:65532"
        for name in (
            "db-provision",
            "migrate",
            "checkpoint-init",
            "storage-init",
            "database-mcp",
            "api",
            "dispatcher",
            "worker",
            "office-converter",
        )
    },
    "edge": "101:101",
    "postgres": "0:0",
    "redis": "0:0",
    "minio": "0:0",
}
_SERVICE_SECRETS = {
    "postgres": {"postgres_owner_password"},
    "redis": {"redis_acl", "redis_app_password"},
    "minio": {"minio_root_access", "minio_root_secret"},
    "db-provision": {
        "postgres_owner_password",
        "postgres_migrator_password",
        "postgres_app_password",
        "postgres_backup_password",
        "postgres_checkpoint_password",
    },
    "migrate": {"postgres_migrator_password", "bootstrap_admin_password"},
    "checkpoint-init": {"postgres_migrator_password"},
    "storage-init": {
        "minio_root_access",
        "minio_root_secret",
        "minio_app_access",
        "minio_app_secret",
        "redis_app_password",
        "postgres_app_password",
    },
    "database-mcp": {"database_mcp_token", "database_readonly_password"},
    "api": {
        "postgres_app_password",
        "minio_app_access",
        "minio_app_secret",
        "redis_app_password",
        "token_signing_key",
        "database_mcp_token",
        "postgres_checkpoint_password",
        "llm_api_key",
        "embedding_api_key",
        "rerank_api_key",
        "ocr_api_key",
        "ocr_secret_id",
        "ocr_secret_key",
        "vision_api_key",
    },
    "dispatcher": {
        "postgres_app_password",
        "minio_app_access",
        "minio_app_secret",
        "redis_app_password",
        "token_signing_key",
        "database_mcp_token",
        "postgres_checkpoint_password",
        "llm_api_key",
        "embedding_api_key",
        "rerank_api_key",
        "ocr_api_key",
        "ocr_secret_id",
        "ocr_secret_key",
        "vision_api_key",
    },
    "worker": {
        "postgres_app_password",
        "minio_app_access",
        "minio_app_secret",
        "redis_app_password",
        "token_signing_key",
        "database_mcp_token",
        "postgres_checkpoint_password",
        "llm_api_key",
        "embedding_api_key",
        "rerank_api_key",
        "ocr_api_key",
        "ocr_secret_id",
        "ocr_secret_key",
        "vision_api_key",
        "converter_token",
    },
    "office-converter": {"converter_token"},
    "edge": {"tls_certificate", "tls_private_key"},
}
_KNOWN_SECRET_IDS = set(_SECRET_IDS) | set(_TLS_UIDS)
_DATA_SERVICES = ("postgres", "redis", "minio")


def check_secret_topology(compose_text: str) -> list[str]:
    """Check fixed service identities, Secret sources, mounts, and readers."""
    try:
        topology = yaml.safe_load(compose_text)
    except (yaml.YAMLError, TypeError):
        return ["Compose Secret topology is invalid"]
    if not isinstance(topology, dict):
        return ["Compose Secret topology is invalid"]
    services, declarations = topology.get("services"), topology.get("secrets")
    if not isinstance(services, dict) or not isinstance(declarations, dict):
        return ["Compose Secret topology is invalid"]

    errors: list[str] = []
    if set(services) != set(_SERVICE_USERS):
        errors.append("Compose services do not match the fixed Secret topology")
    for service_name, expected_user in _SERVICE_USERS.items():
        service = services.get(service_name)
        if not isinstance(service, dict):
            errors.append(f"{service_name} service is missing")
            continue
        if str(service.get("user", "")) != expected_user:
            errors.append(f"{service_name} must run as {expected_user}")
        if "userns_mode" in service:
            errors.append(f"{service_name} cannot override the user namespace")
        if service_name in _DATA_SERVICES:
            caps = service.get("cap_drop", [])
            if not isinstance(caps, list) or any(
                str(cap).upper() in {"ALL", "DAC_OVERRIDE"} for cap in caps
            ):
                errors.append(f"{service_name} root Secret reader capabilities are required")
            if "entrypoint" in service and service["entrypoint"] is not None:
                errors.append(f"{service_name} cannot replace its image entrypoint")
            command = service.get("command")
            first = (
                command.split()[0]
                if isinstance(command, str) and command.split()
                else (command[0] if isinstance(command, list) and command else None)
            )
            expected_first = {
                "postgres": "postgres",
                "redis": "redis-server",
                "minio": "server",
            }[service_name]
            if service_name == "postgres":
                if command is not None and first != "postgres":
                    errors.append("postgres command must retain the image default")
            elif first != expected_first:
                errors.append(f"{service_name} command must retain its image command")

    expected_sources = dict(_SECRET_IDS)
    expected_sources.update({"tls_certificate": None, "tls_private_key": None})
    if set(declarations) != set(expected_sources):
        errors.append("Compose Secret declarations do not match the fixed inventory")
    for secret_id, filename in expected_sources.items():
        declaration = declarations.get(secret_id)
        if not isinstance(declaration, dict) or not isinstance(declaration.get("file"), str):
            errors.append(f"{secret_id} Secret source is invalid")
            continue
        source = declaration["file"]
        if filename is not None:
            expected = f"${{EM_SECRET_DIR:?Set the private Secret directory}}/{filename}"
        elif secret_id == "tls_certificate":
            expected = "${EM_TLS_CERT_FILE:?Set the approved TLS certificate path}"
        else:
            expected = "${EM_TLS_KEY_FILE:?Set the approved TLS private key path}"
        if source != expected:
            errors.append(f"{secret_id} Secret source must use its canonical path")

    for service_name, expected_ids in _SERVICE_SECRETS.items():
        service = services.get(service_name)
        if not isinstance(service, dict):
            continue
        mounts = service.get("secrets", [])
        if not isinstance(mounts, list):
            errors.append(f"{service_name} Secret mounts are invalid")
            continue
        mounted: list[str] = []
        for mount in mounts:
            if isinstance(mount, str):
                source_id, target = mount, None
            elif isinstance(mount, dict):
                source_id = mount.get("source")
                target = mount.get("target")
                fields = set(mount)
                if fields & {"uid", "gid", "mode"}:
                    errors.append(f"{service_name} Secret mount cannot remap ownership or mode")
                if fields - {"source", "target", "uid", "gid", "mode"}:
                    errors.append(f"{service_name} Secret mount fields are unsupported")
            else:
                errors.append(f"{service_name} Secret mount is invalid")
                continue
            if not isinstance(source_id, str) or source_id not in _KNOWN_SECRET_IDS:
                errors.append(f"{service_name} has an unsupported Secret mount")
                continue
            mounted.append(source_id)
            if target is not None and target != f"/run/secrets/{source_id}":
                errors.append(f"{service_name} Secret target must preserve its /run/secrets name")
            source_uid = (
                SECRET_FILE_UIDS[_SECRET_IDS[source_id]]
                if source_id in _SECRET_IDS
                else _TLS_UIDS[source_id]
            )
            service_uid = int(_SERVICE_USERS[service_name].split(":", 1)[0])
            reader_uid = (
                999 if service_name == "redis" and source_id == "redis_acl" else service_uid
            )
            if reader_uid != 0 and source_uid != reader_uid:
                errors.append(
                    f"{service_name} cannot read {source_id} with its configured host owner"
                )
        if len(mounted) != len(set(mounted)):
            errors.append(f"{service_name} has duplicate Secret mounts")
        if set(mounted) != expected_ids:
            errors.append(f"{service_name} Secret mounts do not match the fixed inventory")
    return errors


def check_secret_files(
    secret_dir: Path | None, cert_path: Path | None, key_path: Path | None
) -> list[str]:
    """Check Linux ownership and modes without opening or hashing any file."""
    if not sys.platform.startswith("linux"):
        return ["Secret file ownership checks require Linux metadata"]
    if secret_dir is None or cert_path is None or key_path is None:
        return ["Secret directory, TLS certificate, and private key paths are required"]
    errors: list[str] = []
    inspect_secrets = False
    try:
        directory_stat = secret_dir.lstat()
        if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(directory_stat.st_mode):
            errors.append("Secret directory must be a real directory")
        else:
            inspect_secrets = True
            if directory_stat.st_uid != 0 or stat.S_IMODE(directory_stat.st_mode) != 0o700:
                errors.append("Secret directory must be root-owned mode 0700")
            for child in secret_dir.iterdir():
                try:
                    if stat.S_ISLNK(child.lstat().st_mode):
                        errors.append("Secret directory cannot contain symbolic links")
                except OSError:
                    errors.append("Secret directory entries must be inspectable")
    except OSError:
        errors.append("Secret directory must be inspectable")

    if inspect_secrets:
        for filename, expected_uid in SECRET_FILE_UIDS.items():
            _check_file(secret_dir / filename, filename, expected_uid, {0o400, 0o600}, errors)
    _check_file(cert_path, "TLS certificate", 101, None, errors, certificate=True)
    _check_file(key_path, "TLS private key", 101, {0o400, 0o600}, errors)
    return errors


def _check_file(
    path: Path,
    label: str,
    expected_uid: int,
    allowed_modes: set[int] | None,
    errors: list[str],
    *,
    certificate: bool = False,
) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        errors.append(f"{label} file is missing or unreadable as metadata")
        return
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        errors.append(f"{label} must be a regular non-symlink file")
        return
    if metadata.st_uid != expected_uid:
        errors.append(f"{label} has the wrong owner")
    mode = stat.S_IMODE(metadata.st_mode)
    if certificate:
        if not mode & 0o400 or mode & 0o022:
            errors.append("TLS certificate must be owner-readable without group or other writes")
    elif mode not in allowed_modes:
        errors.append(f"{label} must use mode 0400 or 0600")


def check_daemon_security_options(options: object) -> list[str]:
    """Reject daemon UID remapping, which invalidates the host UID contract."""
    if not isinstance(options, list) or any(not isinstance(item, str) for item in options):
        return ["Docker daemon security options are invalid"]
    for item in options:
        names = re.findall(r"(?:^|[,;\s])name=([^,;\s]+)", item)
        if any(name in {"rootless", "userns"} for name in names):
            return ["Docker daemon rootless or user namespace mode is unsupported"]
    return []
