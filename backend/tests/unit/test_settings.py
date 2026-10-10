from pathlib import Path

import pytest
from pydantic import ValidationError

from enterprise_mind.infrastructure.settings import (
    APISettings,
    AuthConfig,
    ConfigurationError,
    load_application_config,
    read_secret,
)


def test_secret_errors_do_not_expose_path_or_value(tmp_path: Path) -> None:
    secret = tmp_path / "private-secret-name"
    with pytest.raises(ConfigurationError) as error:
        read_secret(secret)
    assert str(secret) not in str(error.value)
    secret.write_text("  preserved spaces  \n", encoding="utf-8")
    value = read_secret(secret)
    assert value.get_secret_value() == "  preserved spaces  "
    assert "preserved" not in repr(value)
    secret.write_text("one\ntwo", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        read_secret(secret)


def test_config_rejects_unknown_keys_and_invalid_ranges(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("auth:\n  unexpected_secret: must-not-appear", encoding="utf-8")
    with pytest.raises(ConfigurationError) as error:
        load_application_config(config)
    assert "must-not-appear" not in str(error.value)
    with pytest.raises(ValidationError):
        AuthConfig(access_token_ttl_seconds=86400)
    with pytest.raises(ValidationError):
        AuthConfig(password_min_length=100, password_max_length=50)


@pytest.mark.parametrize("origin", [None, "http://example.test", "https://a/b", "https://a?x=1"])
def test_production_requires_https_origin(origin: str | None) -> None:
    with pytest.raises(ValidationError):
        APISettings(
            env="production",
            public_origin=origin,
            postgres_host="db",
            postgres_database="em",
            postgres_user="api",
            postgres_password_file=Path("secret"),
            token_signing_key_file=Path("key"),
        )
