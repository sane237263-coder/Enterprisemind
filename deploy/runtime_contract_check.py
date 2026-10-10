"""Check this release topology against application startup requirements.

This reads configuration only. It does not establish network reachability,
Secret readability inside a container, or a successful database bootstrap.
"""

from __future__ import annotations

import posixpath
from typing import Any

import yaml
from enterprise_mind.infrastructure.settings import ApplicationConfig
from pydantic import ValidationError

APPLICATION_ROLES = {
    "enterprise_mind_owner",
    "enterprise_mind_migrator",
    "enterprise_mind_app",
    "enterprise_mind_backup",
    "enterprise_mind_checkpoint",
}


def check_application_contract(application_text: str) -> list[str]:
    """Validate the release schema and deployment-specific startup constraints."""
    try:
        config = ApplicationConfig.model_validate(yaml.safe_load(application_text))
    except (yaml.YAMLError, ValidationError, TypeError, ValueError):
        return ["application config does not match the runtime schema"]
    if config.api.workers != 1:
        return [
            "application config must pin api.workers to 1 for process-local page/export context"
        ]
    if config.office_converter is not None:
        if config.office_converter.base_url not in {
            "http://office-converter:8080",
            "http://office-converter:8080/",
        }:
            return ["office converter must use the internal service origin"]
    return []


def check_runtime_contract(compose_text: str) -> list[str]:
    """Return fixed diagnostic labels; never include supplied Secret values."""
    try:
        topology = yaml.safe_load(compose_text)
        if not isinstance(topology, dict):
            return ["runtime topology must be a mapping"]
        services = topology["services"]
        secrets = topology["secrets"]
        networks = topology["networks"]
        if not all(isinstance(item, dict) for item in (services, secrets, networks)):
            return ["runtime services, secrets and networks must be mappings"]
        errors: list[str] = []

        def service(name: str) -> dict[str, Any]:
            value = services[name]
            if not isinstance(value, dict):
                raise TypeError
            return value

        def environment(name: str) -> dict[str, Any]:
            value = service(name).get("environment", {})
            if not isinstance(value, dict):
                raise TypeError
            return value

        def mounted_secrets(name: str) -> dict[str, str]:
            mounted = {}
            for item in service(name).get("secrets", []):
                if isinstance(item, str):
                    source, target = item, item
                else:
                    source = item["source"]
                    target = item.get("target", source)
                if source not in secrets:
                    raise ValueError
                mounted[posixpath.join("/run/secrets", target)] = source
            return mounted

        def require_secret(name: str, variable: str) -> None:
            if environment(name).get(variable) not in mounted_secrets(name):
                errors.append(f"{name}: {variable} must reference a mounted Secret")

        bootstrap = environment("postgres").get("POSTGRES_USER")
        if not bootstrap or bootstrap in APPLICATION_ROLES:
            errors.append("PostgreSQL bootstrap login must differ from all application roles")
        if environment("db-provision").get("EM_POSTGRES_USER") != bootstrap:
            errors.append("db-provision must use the PostgreSQL bootstrap login")
        require_secret("postgres", "POSTGRES_PASSWORD_FILE")
        require_secret("db-provision", "EM_POSTGRES_PASSWORD_FILE")
        if environment("postgres").get("POSTGRES_PASSWORD_FILE") != environment("db-provision").get(
            "EM_POSTGRES_PASSWORD_FILE"
        ):
            errors.append("db-provision and PostgreSQL must reference the same bootstrap Secret")

        if not environment("migrate").get("EM_BOOTSTRAP_ADMIN_USERNAME"):
            errors.append("migrate must bind the initial administrator username")
        require_secret("migrate", "EM_BOOTSTRAP_ADMIN_PASSWORD_FILE")
        require_secret("api", "EM_TOKEN_SIGNING_KEY_FILE")
        require_secret("api", "EM_CHECKPOINT_POSTGRES_PASSWORD_FILE")
        require_secret("worker", "EM_OFFICE_CONVERTER_TOKEN_FILE")
        require_secret("office-converter", "EM_OFFICE_CONVERTER_TOKEN_FILE")
        if environment("worker").get("EM_OFFICE_CONVERTER_TOKEN_FILE") != environment(
            "office-converter"
        ).get("EM_OFFICE_CONVERTER_TOKEN_FILE"):
            errors.append("Worker and Office converter must reference the same token Secret")

        for name in ("api", "dispatcher", "worker"):
            require_secret(name, "EM_POSTGRES_PASSWORD_FILE")
            if environment(name).get("EM_POSTGRES_USER") != "enterprise_mind_app":
                errors.append(f"{name}: runtime must use the application database login")
            if any(key.startswith("EM_BOOTSTRAP_ADMIN_") for key in environment(name)):
                errors.append(f"{name}: runtime must not receive administrator bootstrap settings")
            if "migrate" in service(name).get("depends_on", {}):
                errors.append(f"{name}: startup must not automatically run migrations")
        if "migrate" in service("checkpoint-init").get("depends_on", {}):
            errors.append("checkpoint-init must not automatically rerun migrations")
        if environment("database-mcp").get("PGSSLROOTCERT") != "/run/config/database-ca.pem":
            errors.append("database-mcp must bind its CA bundle at /run/config/database-ca.pem")

        edge_uid = str(service("edge").get("user", "0")).split(":", 1)[0]
        cache_mounts = [
            mount.split(":", 1)[1]
            for mount in service("edge").get("tmpfs", [])
            if mount.startswith("/var/cache/nginx:")
        ]
        cache_options = (
            dict(option.split("=", 1) for option in cache_mounts[0].split(",") if "=" in option)
            if len(cache_mounts) == 1
            else {}
        )
        if (
            not edge_uid.isdecimal()
            or cache_options.get("uid", "0") != edge_uid
            or int(cache_options.get("mode", "0"), 8) & 0o700 != 0o700
        ):
            errors.append("edge cache tmpfs must be owned and writable by its runtime user")

        for name in ("api", "worker"):
            attached = service(name).get("networks", [])
            if not attached or any(network not in networks for network in attached):
                errors.append(f"{name}: model network is missing or undefined")
            elif not any(
                (networks[network] or {}).get("internal", False) is False for network in attached
            ):
                errors.append(f"{name}: all networks isolate external model endpoints")
        return errors
    except (yaml.YAMLError, KeyError, TypeError, ValueError, AttributeError):
        return ["runtime topology is malformed or uses unsupported configuration shapes"]
