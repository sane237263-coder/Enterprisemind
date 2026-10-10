from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class ConfigurationError(Exception):
    """Safe configuration error; never include a value or secret path."""


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)


class APIConfig(StrictConfig):
    workers: int = Field(default=2, ge=1, le=16)
    regular_request_timeout_seconds: int = Field(default=60, ge=1, le=300)


class AuthConfig(StrictConfig):
    access_token_ttl_seconds: int = Field(default=1800, ge=60, le=3600)
    password_min_length: int = Field(default=12, ge=12, le=128)
    password_max_length: int = Field(default=128, ge=12, le=1024)

    @model_validator(mode="after")
    def check_lengths(self) -> "AuthConfig":
        if self.password_max_length < self.password_min_length:
            raise ValueError("invalid password length range")
        return self


class PostgresConfig(StrictConfig):
    api_pool_max_per_process: int = Field(default=10, ge=1, le=10)
    worker_pool_max: int = Field(default=10, ge=1, le=10)
    scheduler_pool_max: int = Field(default=5, ge=1, le=5)
    connection_timeout_seconds: int = Field(default=5, ge=1, le=30)
    statement_timeout_ms: int = Field(default=10000, ge=1000, le=60000)


class ApplicationConfig(StrictConfig):
    api: APIConfig = Field(default_factory=APIConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    upload: "UploadConfig" = Field(default_factory=lambda: UploadConfig())
    worker: "WorkerConfig" = Field(default_factory=lambda: WorkerConfig())
    outbox: "OutboxConfig" = Field(default_factory=lambda: OutboxConfig())
    storage: "StorageConfig" = Field(default_factory=lambda: StorageConfig())
    phase9: "Phase9Config" = Field(default_factory=lambda: Phase9Config())
    office_converter: "OfficeConverterConfig | None" = None


class OfficeConverterConfig(StrictConfig):
    base_url: str
    timeout_seconds: int = Field(default=180, ge=1, le=600)

    @model_validator(mode="after")
    def endpoint(self) -> "OfficeConverterConfig":
        from urllib.parse import urlsplit

        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("converter must use a configured internal origin")
        return self


class UploadConfig(StrictConfig):
    max_bytes: int = Field(default=104857600, ge=1, le=104857600)
    max_files: int = Field(default=10, ge=1, le=20)
    request_timeout_seconds: int = Field(default=300, ge=10, le=1800)
    allowed_extensions: list[
        Literal[
            "pdf",
            "md",
            "markdown",
            "txt",
            "csv",
            "doc",
            "docx",
            "ppt",
            "pptx",
            "xls",
            "xlsx",
            "png",
            "jpg",
            "jpeg",
            "webp",
        ]
    ] = [
        "pdf",
        "md",
        "markdown",
        "txt",
        "csv",
        "doc",
        "docx",
        "ppt",
        "pptx",
        "xls",
        "xlsx",
        "png",
        "jpg",
        "jpeg",
        "webp",
    ]


class WorkerConfig(StrictConfig):
    concurrency: int = Field(default=2, ge=1, le=8)
    lease_seconds: int = Field(default=120, ge=10, le=3600)
    lease_renew_interval_seconds: int = Field(default=30, ge=1, le=300)
    parse_timeout_seconds: int = Field(default=180, ge=1, le=600)
    parse_memory_limit_mb: int = Field(default=768, ge=128, le=2048)
    max_source_units: int = Field(default=10000, ge=1, le=20000)
    max_elements: int = Field(default=100000, ge=1, le=500000)
    max_text_chars: int = Field(default=20000000, ge=100, le=100000000)
    max_archive_bytes: int = Field(default=268435456, ge=1024, le=536870912)
    max_archive_members: int = Field(default=20000, ge=1, le=100000)
    max_image_pixels: int = Field(default=25000000, ge=1024, le=100000000)
    image_max_edge: int = Field(default=2048, ge=128, le=4096)
    max_artifact_bytes: int = Field(default=104857600, ge=1024, le=268435456)

    @model_validator(mode="after")
    def valid_lease(self) -> "WorkerConfig":
        if self.lease_renew_interval_seconds * 2 >= self.lease_seconds:
            raise ValueError("renewal interval must be less than half the lease")
        return self


class OutboxConfig(StrictConfig):
    poll_interval_seconds: int = Field(default=1, ge=1, le=60)
    claim_batch_size: int = Field(default=100, ge=1, le=1000)
    published_retention_days: int = Field(default=7, ge=1, le=30)
    redelivery_seconds: int = Field(default=120, ge=5, le=3600)


class StorageConfig(StrictConfig):
    orphan_grace_seconds: int = Field(default=86400, ge=3600, le=604800)
    connection_timeout_seconds: int = Field(default=5, ge=1, le=30)
    read_timeout_seconds: int = Field(default=30, ge=1, le=120)


class Phase9Config(StrictConfig):
    """Centralized engineering budgets for Memory and generated files."""

    memory_max_chars: int = Field(default=1000, ge=100, le=1000)
    memory_auto_candidates: int = Field(default=3, ge=0, le=10)
    memory_select_candidates: int = Field(default=50, ge=1, le=500)
    memory_inject_items: int = Field(default=10, ge=1, le=50)
    memory_inject_chars: int = Field(default=6000, ge=500, le=50000)
    export_max_rows: int = Field(default=1_000_000, ge=1, le=10_000_000)
    export_fetch_batch_size: int = Field(default=1000, ge=1, le=1000)
    export_max_serialized_bytes: int = Field(default=100 * 1024 * 1024, ge=1, le=500 * 1024 * 1024)
    generation_max_file_bytes: int = Field(default=100 * 1024 * 1024, ge=1, le=500 * 1024 * 1024)
    generation_temp_max_bytes: int = Field(default=300 * 1024 * 1024, ge=1, le=1024 * 1024 * 1024)
    database_export_timeout_seconds: int = Field(default=120, ge=1, le=600)
    generation_timeout_seconds: int = Field(default=120, ge=1, le=600)
    generation_recovery_max_attempts: int = Field(default=3, ge=1, le=10)


ApplicationConfig.model_rebuild()


class IngestionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EM_", extra="ignore", hide_input_in_errors=True)
    minio_endpoint: str
    minio_bucket: str = "enterprise-mind"
    minio_access_key_file: Path = Field(repr=False)
    minio_secret_key_file: Path = Field(repr=False)
    minio_secure: bool = False
    redis_host: str
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_username: str = "enterprise_mind_app"
    redis_password_file: Path = Field(repr=False)
    redis_stream: str = "enterprise-mind:processing"

    @model_validator(mode="after")
    def valid_endpoints(self) -> "IngestionSettings":
        from urllib.parse import urlsplit

        endpoint = urlsplit("//" + self.minio_endpoint)
        if (
            not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.path
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("MinIO endpoint must be host[:port]")
        if not self.redis_stream.startswith("enterprise-mind:"):
            raise ValueError("invalid queue namespace")
        return self


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EM_", extra="ignore", hide_input_in_errors=True)

    env: Literal["production", "development", "test"]
    postgres_host: str = Field(min_length=1)
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_database: str = Field(min_length=1)
    postgres_user: str = Field(min_length=1)
    postgres_password_file: Path = Field(repr=False)
    application_config: Path = Path("config/application.yaml")

    def database_url(self, *, asynchronous: bool = False) -> URL:
        return URL.create(
            "postgresql+asyncpg" if asynchronous else "postgresql+psycopg",
            username=self.postgres_user,
            password=read_secret(self.postgres_password_file).get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_database,
        )


class APISettings(DatabaseSettings):
    token_signing_key_file: Path = Field(repr=False)
    public_origin: str | None = None

    @model_validator(mode="after")
    def production_origin(self) -> "APISettings":
        from urllib.parse import urlsplit

        if self.env == "production":
            parsed = urlsplit(self.public_origin or "")
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("EM_PUBLIC_ORIGIN must be a single HTTPS origin")
        return self


class BootstrapSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EM_", extra="ignore", hide_input_in_errors=True)
    bootstrap_admin_username: str | None = None
    bootstrap_admin_password_file: Path | None = Field(default=None, repr=False)


def read_secret(path: Path) -> SecretStr:
    try:
        # A trailing line ending from a mounted text Secret is not part of the value.
        value = path.read_text(encoding="utf-8").removesuffix("\n").removesuffix("\r")
    except (OSError, UnicodeError):
        raise ConfigurationError("无法读取必需的 Secret 文件。") from None
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise ConfigurationError("Secret 必须是非空单行文本。")
    return SecretStr(value)


def load_application_config(path: Path) -> ApplicationConfig:
    try:
        return ApplicationConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError):
        raise ConfigurationError("应用配置文件缺失或不符合配置结构。") from None
