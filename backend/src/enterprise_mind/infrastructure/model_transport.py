"""Explicit transport settings for model APIs; do not inherit unrelated system proxies."""

import os
from urllib.parse import urlsplit

from enterprise_mind.infrastructure.settings import ConfigurationError


def model_proxy_url() -> str | None:
    value = os.environ.get("EM_MODEL_PROXY_URL")
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port == 0
            or any(character.isspace() for character in value)
        ):
            raise ValueError("invalid proxy")
    except ValueError:
        raise ConfigurationError("模型代理必须是无账号密码的 HTTP(S) 地址。") from None
    return value
