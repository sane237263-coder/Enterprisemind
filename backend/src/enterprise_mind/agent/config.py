from pathlib import Path

import yaml
from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from enterprise_mind.infrastructure.settings import ConfigurationError, StrictConfig


class AgentConfig(StrictConfig):
    attachment_ttl_seconds: int = Field(default=86400, ge=60, le=604800)
    pending_action_ttl_seconds: int = Field(default=900, ge=60, le=86400)
    max_tool_calls: int = Field(default=12, ge=1, le=50)
    max_replans: int = Field(default=2, ge=0, le=5)
    tool_max_retries: int = Field(default=3, ge=0, le=3)
    concurrency: int = Field(default=2, ge=1, le=4)
    tool_concurrency: int = Field(default=3, ge=1, le=6)
    poll_interval_seconds: float = Field(default=1, gt=0, le=30)
    run_timeout_seconds: int = Field(default=600, ge=10, le=1800)
    summary_threshold_tokens: int = Field(default=6000, ge=100, le=100000)
    recent_message_count: int = Field(default=12, ge=2, le=50)
    max_message_chars: int = Field(default=16000, ge=100, le=50000)
    max_answer_chars: int = Field(default=50000, ge=100, le=100000)
    sse_poll_seconds: float = Field(default=1, gt=0, le=10)


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EM_", extra="ignore", hide_input_in_errors=True)
    agent_config: Path = Path(__file__).resolve().parents[4] / "config/agent.yaml"
    checkpoint_postgres_user: str = "enterprise_mind_checkpoint"
    checkpoint_postgres_password_file: Path | None = None


def load_agent(settings: AgentSettings) -> AgentConfig:
    try:
        data = yaml.safe_load(settings.agent_config.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"agent"}:
            raise ValueError("invalid configuration")
        return AgentConfig.model_validate(data["agent"])
    except (OSError, ValueError, TypeError, yaml.YAMLError, ValidationError):
        raise ConfigurationError("Agent 配置缺失或不符合要求。") from None
