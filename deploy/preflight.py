"""Fail-closed release preflight for the EnterpriseMind Compose topology.

This script validates release metadata and target file identities without
printing Secret values.  A successful ``--static`` run means only that the
candidate materials are structurally consistent.  A production run also
requires a Linux target, immutable image references, readable Secret files,
an approved HTTPS origin, a private MCP configuration and a real certificate
pair; it is intentionally impossible to turn the repository template into a
production pass by accepting placeholders.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import posixpath
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from config_access_check import check_config_access, resolve_config_file
from database_tls_check import (
    DATABASE_CA_FILE,
    check_database_ca,
    check_database_tls_config,
)
from provider_contract_check import check_provider_contract
from runtime_contract_check import check_application_contract
from secret_contract_check import (
    SECRET_FILE_UIDS,
    check_daemon_security_options,
    check_secret_files,
    check_secret_topology,
)
from tls_contract_check import check_tls_contract

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose.yaml"
OFFICE_COMPOSE_FILE = REPO_ROOT / "deploy" / "compose.office.yaml"
REQUIRED_SECRET_FILES = tuple(SECRET_FILE_UIDS)
REQUIRED_ENV = (
    "EM_COMPOSE_PROJECT",
    "EM_PUBLIC_ORIGIN",
    "EM_PUBLIC_HOST",
    "EM_EDGE_IP",
    "EM_EDGE_BIND",
    "EM_SECRET_DIR",
    "EM_CONFIG_DIR",
    "EM_TLS_CERT_FILE",
    "EM_TLS_KEY_FILE",
    "EM_PYTHON_IMAGE",
    "EM_UV_IMAGE",
    "EM_NODE_IMAGE",
    "EM_NGINX_IMAGE",
    "EM_POSTGRES_IMAGE",
    "EM_REDIS_IMAGE",
    "EM_MINIO_IMAGE",
    "EM_MINIO_BUCKET",
    "EM_BOOTSTRAP_ADMIN_USERNAME",
)
IMAGE_ENV = (
    "EM_PYTHON_IMAGE",
    "EM_UV_IMAGE",
    "EM_NODE_IMAGE",
    "EM_NGINX_IMAGE",
    "EM_POSTGRES_IMAGE",
    "EM_REDIS_IMAGE",
    "EM_MINIO_IMAGE",
)
PLACEHOLDERS = ("CHANGE_ME", "RELEASE_ID", "example.invalid", "TODO", "<", ">")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_CANONICAL_PORT = re.compile(r"[1-9][0-9]{0,4}")


class CheckResult:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.passed: list[str] = []

    def ok(self, name: str) -> None:
        self.passed.append(name)

    def fail(self, name: str) -> None:
        self.errors.append(name)

    def warn(self, name: str) -> None:
        self.warnings.append(name)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _contains_placeholder(value: str) -> bool:
    return any(item in value for item in PLACEHOLDERS)


def _has_whitespace_or_control(value: str) -> bool:
    return any(char.isspace() or unicodedata.category(char) == "Cc" for char in value)


def _normalize_public_hostname(host: str) -> str | None:
    """Return a canonical IPv4/IPv6 address or ASCII DNS hostname."""
    if not host or host.endswith(".") or "%" in host or "\\" in host:
        return None

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if getattr(address, "scope_id", None) is not None:
            return None
        return address.compressed.lower()

    # Do not reinterpret malformed or non-canonical numeric IPv4 forms as DNS.
    if re.fullmatch(r"[0-9.]+", host) or len(host) > 253:
        return None
    labels = host.split(".")
    if any(len(label) > 63 or not _DNS_LABEL.fullmatch(label) for label in labels):
        return None
    return host.lower()


def _configured_https_port() -> int | None:
    """Match Compose's ${EM_HTTPS_PORT:-443} default and reject ambiguous ports."""
    raw_port = os.environ.get("EM_HTTPS_PORT")
    if raw_port is None or raw_port == "":
        return 443
    if not _CANONICAL_PORT.fullmatch(raw_port):
        return None
    port = int(raw_port)
    return port if port <= 65535 else None


def _origin_port(authority: str) -> tuple[bool, int | None]:
    """Return whether an authority has a valid explicit port and its value."""
    if not authority or "@" in authority or "\\" in authority:
        return False, None

    if authority.startswith("["):
        closing = authority.find("]")
        if closing <= 1 or "[" in authority[1:] or "]" in authority[closing + 1 :]:
            return False, None
        suffix = authority[closing + 1 :]
        if not suffix:
            return True, None
        if not suffix.startswith(":"):
            return False, None
        raw_port = suffix[1:]
    else:
        if "[" in authority or "]" in authority or authority.count(":") > 1:
            return False, None
        if ":" not in authority:
            return True, None
        raw_port = authority.rsplit(":", 1)[1]

    if not _CANONICAL_PORT.fullmatch(raw_port):
        return False, None
    port = int(raw_port)
    return (port <= 65535, port if port <= 65535 else None)


def _check_public_origin(result: CheckResult) -> None:
    origin = os.environ.get("EM_PUBLIC_ORIGIN", "")
    public_host = os.environ.get("EM_PUBLIC_HOST", "")
    configured_port = _configured_https_port()
    if configured_port is None:
        result.fail("EM_HTTPS_PORT is not a valid TCP port")
    if not origin:
        return

    origin_error = "EM_PUBLIC_ORIGIN is not an approved concrete HTTPS origin"
    if _has_whitespace_or_control(origin) or "\\" in origin or _contains_placeholder(origin):
        result.fail(origin_error)
        return

    try:
        parsed = urlsplit(origin)
        hostname = parsed.hostname
        port_valid, origin_port = _origin_port(parsed.netloc)
        normalized_origin_host = _normalize_public_hostname(hostname or "")
        bracketed_host_is_ipv6 = True
        if parsed.netloc.startswith("["):
            try:
                bracketed_host_is_ipv6 = isinstance(
                    ipaddress.ip_address(hostname or ""), ipaddress.IPv6Address
                )
            except ValueError:
                bracketed_host_is_ipv6 = False
        origin_valid = (
            parsed.scheme == "https"
            and parsed.netloc != ""
            and "@" not in parsed.netloc
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and "?" not in origin
            and "#" not in origin
            and port_valid
            and normalized_origin_host is not None
            and bracketed_host_is_ipv6
        )
    except ValueError:
        # URL parsing errors must stay redacted and must never escape as tracebacks.
        result.fail(origin_error)
        return

    if not origin_valid:
        result.fail(origin_error)
        return
    result.ok("public origin is HTTPS and has no path/query/credentials")

    try:
        normalized_public_host = _normalize_public_hostname(public_host)
    except ValueError:
        normalized_public_host = None
    if normalized_public_host is None:
        result.fail("EM_PUBLIC_HOST is not a valid DNS or IP hostname")
    elif normalized_origin_host != normalized_public_host:
        result.fail("EM_PUBLIC_HOST does not match EM_PUBLIC_ORIGIN hostname")
    else:
        result.ok("public host matches HTTPS origin")

    effective_origin_port = origin_port if origin_port is not None else 443
    if configured_port is not None:
        if effective_origin_port != configured_port:
            result.fail("EM_PUBLIC_ORIGIN port does not match EM_HTTPS_PORT")
        else:
            result.ok("public origin port matches EM_HTTPS_PORT")


def _check_env(result: CheckResult, *, static: bool) -> None:
    missing = [name for name in REQUIRED_ENV if not _env(name)]
    if missing:
        result.fail("missing environment fields: " + ",".join(missing))
    else:
        result.ok("required environment fields are present")

    project = _env("EM_COMPOSE_PROJECT")
    if project and not re.fullmatch(r"enterprise-mind-prod-[a-z0-9][a-z0-9-]{2,62}", project):
        result.fail("EM_COMPOSE_PROJECT must be an isolated enterprise-mind-prod-* name")
    elif project:
        result.ok("Compose project identity is scoped")

    for name in IMAGE_ENV:
        value = _env(name)
        if not value:
            continue
        if _contains_placeholder(value) or ":latest" in value:
            result.fail(f"{name} contains a placeholder or floating latest tag")
            continue
        if not re.search(r"@sha256:[0-9a-f]{64}$", value):
            result.fail(f"{name} must end in a 64-hex sha256 digest")
        else:
            result.ok(f"{name} is digest pinned")

    _check_public_origin(result)

    bind = _env("EM_EDGE_BIND")
    if bind and (bind in {"0.0.0.0", "::"} or _contains_placeholder(bind)):
        result.fail("EM_EDGE_BIND is broad or still a placeholder")
    elif bind:
        result.ok("edge bind is explicit")

    edge_ip = _env("EM_EDGE_IP")
    if edge_ip:
        try:
            ipaddress.ip_address(edge_ip)
        except ValueError:
            result.fail("EM_EDGE_IP is not a valid IP address")
        else:
            if bind and bind not in {edge_ip, "127.0.0.1", "::1"}:
                result.fail("EM_EDGE_BIND must equal EM_EDGE_IP or be an explicit loopback bind")
            else:
                result.ok("edge bind and advertised edge IP are consistent")

    bucket = _env("EM_MINIO_BUCKET")
    if bucket and (
        not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,62}", bucket) or _contains_placeholder(bucket)
    ):
        result.fail("EM_MINIO_BUCKET is not a concrete dedicated bucket")
    elif bucket:
        result.ok("MinIO bucket is concrete")

    administrator = _env("EM_BOOTSTRAP_ADMIN_USERNAME")
    if administrator and (_contains_placeholder(administrator) or len(administrator) > 100):
        result.fail("initial administrator username is invalid or still a placeholder")

    if static:
        result.warn("static mode: target platform, files, certificate and Docker were not checked")


def _check_paths(result: CheckResult, *, static: bool) -> dict[str, str]:
    paths = {
        name: Path(_env(name)).expanduser()
        for name in (
            "EM_SECRET_DIR",
            "EM_CONFIG_DIR",
            "EM_TLS_CERT_FILE",
            "EM_TLS_KEY_FILE",
        )
        if _env(name)
    }
    if static:
        return {name: str(path) for name, path in paths.items()}

    if sys.platform != "linux":
        result.fail("production preflight must run on the approved Linux host")
    else:
        result.ok("target platform is Linux")

    for name in ("EM_TLS_CERT_FILE", "EM_TLS_KEY_FILE"):
        path = paths.get(name)
        if path is None:
            continue
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            result.fail(f"{name} path cannot be resolved")
            continue
        if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
            result.fail(f"{name} must remain outside the repository")

    secret_dir = paths.get("EM_SECRET_DIR")
    if secret_dir is not None and secret_dir.is_dir():
        if REPO_ROOT in secret_dir.resolve().parents or secret_dir.resolve() == REPO_ROOT:
            result.fail("Secret directory must remain outside the repository")
    secret_errors = check_secret_files(
        secret_dir, paths.get("EM_TLS_CERT_FILE"), paths.get("EM_TLS_KEY_FILE")
    )
    for error in secret_errors:
        result.fail(error)
    if not secret_errors:
        result.ok("Secret and TLS file ownership/modes match the declared container readers")

    config_hashes: dict[str, str] = {}
    config_dir = paths.get("EM_CONFIG_DIR")
    runtime_files: dict[str, tuple[str, ...]] = {}
    config_root: Path | None = None
    if config_dir is not None:
        try:
            config_root = config_dir.resolve(strict=True)
            if not config_root.is_dir():
                config_root = None
        except (OSError, RuntimeError):
            config_root = None
    if config_root is None:
        result.fail("EM_CONFIG_DIR does not identify a directory")
    else:
        if REPO_ROOT in config_root.parents or config_root == REPO_ROOT:
            result.fail("release config directory must remain outside the repository")
        config_inventory_complete = True
        for name in (
            "application.yaml",
            "database.yaml",
            "database-mcp-server.yaml",
            "providers.yaml",
            "rag.yaml",
        ):
            try:
                path = resolve_config_file(config_dir, (name,))
                runtime_files[f"config/{name}"] = (name,)
                config_hashes[f"config/{name}"] = _sha256(path)
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError, RuntimeError):
                result.fail(f"deployment config cannot be read: {name}")
                config_inventory_complete = False
                continue
            except ValueError:
                result.fail(f"deployment config must resolve within its container mount: {name}")
                config_inventory_complete = False
                continue
            if _contains_placeholder(text):
                result.fail(f"deployment config still contains a placeholder: {name}")
            else:
                result.ok(f"deployment config is concrete: {name}")
            if name == "application.yaml":
                application_errors = check_application_contract(text)
                for error in application_errors:
                    result.fail(error)
                if application_errors:
                    config_inventory_complete = False
                else:
                    result.ok("application config matches the runtime schema and single API worker")
            if name == "providers.yaml":
                provider_errors, tokenizer_hashes = check_provider_contract(
                    text, config_dir, providers_path=path, tokenizer_paths=runtime_files
                )
                for error in provider_errors:
                    result.fail(error)
                if provider_errors:
                    config_inventory_complete = False
                config_hashes.update(tokenizer_hashes)
                if not provider_errors:
                    result.ok("required providers and release tokenizer paths/hashes passed")
            if name == "database-mcp-server.yaml":
                for error in check_database_tls_config(text):
                    result.fail(error)
        try:
            ca_path = resolve_config_file(config_dir, (DATABASE_CA_FILE,))
            runtime_files[f"config/{DATABASE_CA_FILE}"] = (DATABASE_CA_FILE,)
            ca_errors = check_database_ca(ca_path)
            if ca_errors:
                config_inventory_complete = False
                for error in ca_errors:
                    result.fail(error)
            else:
                config_hashes[f"config/{DATABASE_CA_FILE}"] = _sha256(ca_path)
                result.ok("database CA bundle loads and contains a CA certificate")
        except (OSError, RuntimeError, ValueError):
            config_inventory_complete = False
            result.fail("database CA bundle must be a readable file within the config mount")
        access_errors = check_config_access(config_dir, runtime_files)
        for error in access_errors:
            result.fail(error)
        if not access_errors and config_inventory_complete:
            result.ok("release configs and tokenizers are readable as UID/GID 65532")
    tls_errors = check_tls_contract(
        paths.get("EM_TLS_CERT_FILE"),
        paths.get("EM_TLS_KEY_FILE"),
        _env("EM_PUBLIC_HOST"),
    )
    for error in tls_errors:
        result.fail(error)
    if not tls_errors:
        result.ok("TLS leaf validity, exact SAN and certificate/key pairing passed")
    result.warn("TLS checks do not verify chain trust, revocation or a live HTTPS handshake")
    result.warn("host ownership checks do not replace final-image Secret reads or mount validation")
    result.warn(
        "config access checks do not replace final container mounts or security policy checks"
    )
    result.warn(
        "database CA loading does not verify the actual server chain, hostname or revocation"
    )
    return config_hashes


def _service_block(compose: str, service: str) -> str:
    match = re.search(rf"(?ms)^  {re.escape(service)}:\n(.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)", compose)
    return match.group(1) if match else ""


def _check_postgres_image(result: CheckResult) -> None:
    image = _env("EM_POSTGRES_IMAGE")
    if not image:
        result.fail("cannot inspect PostgreSQL image without EM_POSTGRES_IMAGE")
        return
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", image],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        result.fail("PostgreSQL image inspection is unavailable on this target")
        return
    if completed.returncode:
        result.fail("locked PostgreSQL image is not available for metadata inspection")
        return
    try:
        inspected = json.loads(completed.stdout)[0]
        config = inspected["Config"]
        image_env = {
            value.split("=", 1)[0]: value.split("=", 1)[1]
            for value in config.get("Env", [])
            if "=" in value
        }
        pgdata = image_env.get("PGDATA")
        declared_volumes = set(config.get("Volumes") or {})
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        result.fail("locked PostgreSQL image metadata is invalid")
        return

    data_root = "/var/lib/postgresql"
    normalized_pgdata = posixpath.normpath(pgdata) if pgdata else ""
    if not normalized_pgdata.startswith(data_root + "/"):
        result.fail("PostgreSQL image PGDATA is outside /var/lib/postgresql")
    elif data_root not in declared_volumes:
        result.fail("PostgreSQL image does not declare /var/lib/postgresql as a volume")
    else:
        result.ok("locked PostgreSQL PGDATA is inside its declared volume root")


def _check_docker_secret_namespace(result: CheckResult) -> None:
    """The file-owner contract assumes an unmapped, rootful Docker daemon."""
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{json .SecurityOptions}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode:
            result.fail("Docker identity namespace could not be inspected")
            return
        options = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        result.fail("Docker identity namespace could not be inspected")
        return
    errors = check_daemon_security_options(options)
    for error in errors:
        result.fail(error)
    if not errors:
        result.ok("Docker daemon uses the host UID mapping required by file-backed Secrets")


def _check_compose(result: CheckResult, *, run_docker: bool) -> None:
    if not COMPOSE_FILE.is_file():
        result.fail("deploy/compose.yaml is missing")
        return
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    secret_errors = check_secret_topology(compose)
    for error in secret_errors:
        result.fail(error)
    if not secret_errors:
        result.ok("Compose Secret readers, source paths and numeric identities match")
    try:
        from runtime_contract_check import check_runtime_contract
    except ImportError:
        result.fail("runtime contract checker requires the locked backend Python environment")
    else:
        runtime_errors = check_runtime_contract(compose)
        for error in runtime_errors:
            result.fail(error)
        if not runtime_errors:
            result.ok("Compose initialization and runtime configuration contracts passed")
    for service in ("postgres", "redis", "minio", "database-mcp"):
        block = _service_block(compose, service)
        if not block:
            result.fail(f"required service is missing: {service}")
        elif re.search(r"(?m)^    ports:", block):
            result.fail(f"private service publishes a host port: {service}")
        else:
            result.ok(f"private service has no published host port: {service}")
    postgres_block = _service_block(compose, "postgres")
    postgres_mounts = re.findall(r"(?m)^\s+-\s+postgres_data:(\S+)\s*$", postgres_block)
    if postgres_mounts == ["/var/lib/postgresql"]:
        result.ok("PostgreSQL named volume covers the PGDATA root")
    else:
        result.fail(
            "PostgreSQL named volume must mount /var/lib/postgresql, "
            f"found: {postgres_mounts or 'none'}"
        )
    if re.search(r"(?m)^\s+-\s+\.\.?[:/]", compose):
        result.fail("Compose contains a development source bind mount")
    else:
        result.ok("Compose has no development source bind mount")
    if "latest" in compose.lower():
        result.fail("Compose contains a floating latest image reference")
    else:
        result.ok("Compose has no latest image reference")

    if not run_docker:
        result.warn("Docker Compose config and locked image metadata were not requested")
        return
    _check_docker_secret_namespace(result)
    _check_postgres_image(result)
    project = _env("EM_COMPOSE_PROJECT")
    if not project:
        result.fail("cannot render Compose without EM_COMPOSE_PROJECT")
        return
    try:
        completed = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "-p",
                project,
                "config",
                "--quiet",
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        result.fail("Docker Compose is unavailable on this target")
        return
    if completed.returncode:
        result.fail("Docker Compose config rendering failed")
    else:
        result.ok("Docker Compose config rendering passed")
    # Validate the optional topology too: Office must become a required healthy
    # dependency when enabled, without making it mandatory in the base topology.
    try:
        completed = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "-f",
                str(OFFICE_COMPOSE_FILE),
                "--profile",
                "office",
                "-p",
                project,
                "config",
                "--quiet",
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        result.fail("Office-enabled Docker Compose is unavailable on this target")
        return
    if completed.returncode:
        result.fail("Office-enabled Docker Compose config rendering failed")
    else:
        result.ok("Office-enabled Docker Compose config rendering passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--static",
        action="store_true",
        help="check repository materials without target files",
    )
    parser.add_argument(
        "--compose-check",
        action="store_true",
        help="inspect Docker UID mapping, locked image metadata and Compose config",
    )
    parser.add_argument("--evidence", type=Path, help="write redacted JSON evidence to this path")
    args = parser.parse_args()

    result = CheckResult()
    if not args.static and not args.compose_check:
        result.fail("production preflight requires --compose-check to verify Docker UID mapping")
    _check_env(result, static=args.static)
    hashes = _check_paths(result, static=args.static)
    _check_compose(result, run_docker=args.compose_check)

    config_hashes = {} if args.static else hashes
    resolved_paths = hashes if args.static else {}

    payload = {
        "script": "deploy/preflight.py",
        "mode": "static" if args.static else "production",
        "platform": sys.platform,
        "compose_file": str(COMPOSE_FILE.relative_to(REPO_ROOT)),
        "config_sha256": config_hashes,
        "resolved_paths": resolved_paths,
        "passed": result.passed,
        "warnings": result.warnings,
        "errors": result.errors,
        "status": "PASS" if not result.errors else "BLOCKED",
    }
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not result.errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
