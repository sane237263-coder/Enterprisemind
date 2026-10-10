"""Explicit internal MCP configuration; passwords are resolved only in the MCP process."""

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import Field, field_validator

from enterprise_mind.infrastructure.settings import ConfigurationError, StrictConfig

PROTOCOL_VERSION = "2026-07-28"
AUTH_HEADER = "X-EnterpriseMind-MCP-Secret"


class ConnectionOptions(StrictConfig):
    connect_timeout: int = Field(default=5, ge=1, le=5)
    application_name: str = Field(
        default="enterprise_mind_database_mcp", min_length=1, max_length=63
    )
    target_session_attrs: Literal["any", "primary", "standby", "read-write", "read-only"] = "any"

    @field_validator("application_name")
    @classmethod
    def application_value(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("invalid connection option")
        return value


class DatabaseConnectionConfig(StrictConfig):
    host: str
    port: int = Field(default=5432, ge=1, le=65535)
    database_name: str
    user: str
    password_file: Path = Field(repr=False)
    sslmode: Literal["disable", "require", "verify-ca", "verify-full"] = "verify-full"
    secret_ref: str = "secret:enterprise_database_readonly"
    connection_options: dict[str, object] | None = None

    @field_validator("connection_options")
    @classmethod
    def options_value(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        if value is None:
            return None
        return ConnectionOptions.model_validate(value).model_dump(exclude_unset=True)

    @field_validator("host", "database_name", "user", "secret_ref")
    @classmethod
    def text_value(cls, value: str) -> str:
        if not value.strip() or any(c in value for c in "\r\n\x00"):
            raise ValueError("invalid database metadata")
        return value


class DatabaseMCPConfig(StrictConfig):
    endpoint: str
    auth_secret_file: Path = Field(repr=False)

    @field_validator("endpoint")
    @classmethod
    def endpoint_value(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path != "/mcp"
        ):
            raise ValueError("expected a configured internal MCP endpoint")
        return value


class DatabaseMCPServerConfig(StrictConfig):
    database: DatabaseConnectionConfig
    auth_secret_file: Path = Field(repr=False)
    listen_host: str = "0.0.0.0"
    listen_port: int = Field(default=8000, ge=1, le=65535)
    allowed_hosts: list[str] = Field(min_length=1)
    allowed_origins: list[str] = Field(default_factory=list)


def load_database() -> DatabaseMCPConfig | None:
    path = Path(
        os.environ.get(
            "EM_DATABASE_CONFIG", str(Path(__file__).resolve().parents[4] / "config/database.yaml")
        )
    )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"database_mcp"}:
            raise ValueError
        if raw["database_mcp"] is None:
            return None
        data = dict(raw["database_mcp"])
        data["auth_secret_file"] = Path(data["auth_secret_file"])
        return DatabaseMCPConfig.model_validate(data)
    except Exception:
        raise ConfigurationError("Database MCP 客户端配置无效。") from None


def load_server(path: Path) -> DatabaseMCPServerConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["auth_secret_file"] = Path(data["auth_secret_file"])
        data["database"]["password_file"] = Path(data["database"]["password_file"])
        return DatabaseMCPServerConfig.model_validate(data)
    except Exception:
        raise ConfigurationError("Database MCP 服务配置无效。") from None
