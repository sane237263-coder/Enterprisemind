"""Offline checks for the enterprise database's client trust configuration."""

from __future__ import annotations

import ssl
from pathlib import Path

import yaml

DATABASE_CA_FILE = "database-ca.pem"


def check_database_tls_config(config_text: str) -> list[str]:
    """Require TLS identity verification and a read-only database session."""
    try:
        config = yaml.safe_load(config_text)
        database = config.get("database") if isinstance(config, dict) else None
        if not isinstance(database, dict):
            return ["database MCP connection configuration is invalid"]
        errors = []
        if database.get("sslmode") != "verify-full":
            errors.append("database MCP must require sslmode verify-full")
        options = database.get("connection_options")
        if not isinstance(options, dict) or options.get("target_session_attrs") != "read-only":
            errors.append("MCP config must require target_session_attrs: read-only")
        return errors
    except (yaml.YAMLError, TypeError, ValueError):
        return ["database MCP connection configuration is invalid"]


def check_database_ca(path: Path) -> list[str]:
    """Load a public CA bundle; no handshake, target trust or revocation claim."""
    try:
        if not path.is_file():
            return ["database CA bundle is missing or invalid"]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=str(path))
        if context.cert_store_stats()["x509_ca"] < 1:
            return ["database CA bundle must contain a CA certificate"]
    except (OSError, ValueError, TypeError, ssl.SSLError):
        return ["database CA bundle is missing or invalid"]
    return []
