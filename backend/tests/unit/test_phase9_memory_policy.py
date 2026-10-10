import pytest

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.memory.service import (
    content_hash,
    normalized_content,
    sanitize_for_extraction,
    validate_category,
)


def test_memory_rejects_secrets_and_bounds_content() -> None:
    with pytest.raises(APIError) as error:
        normalized_content("password: super-secret")
    assert error.value.code == ErrorCode.VALIDATION_ERROR
    with pytest.raises(APIError):
        normalized_content("x" * 1001)


def test_memory_category_and_hash_are_deterministic() -> None:
    assert validate_category("preference") == "preference"
    assert content_hash("中文偏好") == content_hash("中文偏好")
    with pytest.raises(APIError):
        validate_category("enterprise_fact")


def test_memory_extraction_redacts_known_credentials_before_model_call() -> None:
    for value, fragment in (
        ("我偏好先给结论，password=fixture-password", "fixture-password"),
        ("我偏好先给结论，token=fixture-token", "fixture-token"),
        ("我偏好先给结论，api_key=fixture-api-key", "fixture-api-key"),
        ("我偏好先给结论，secret=fixture-secret", "fixture-secret"),
        (
            "我偏好先给结论，postgresql://fixture:fixture-pass@localhost/business",
            "fixture-pass",
        ),
        (
            "-----BEGIN PRIVATE KEY-----\nfixture-private-key\n"
            "-----END PRIVATE KEY-----，我偏好先给结论",
            "fixture-private-key",
        ),
    ):
        sanitized = sanitize_for_extraction(value)
        assert fragment not in sanitized
        assert "先给结论" in sanitized
    assert sanitize_for_extraction("-----BEGIN PRIVATE KEY-----\nsecret") == "[敏感内容已移除]"
