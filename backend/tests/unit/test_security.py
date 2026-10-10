from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import SecretStr

from enterprise_mind.identity.security import Passwords, Tokens, normalize_name, parse_id
from enterprise_mind.infrastructure.settings import AuthConfig, ConfigurationError


@pytest.mark.parametrize(
    "value", ["0", "-1", "01", "+1", "1.0", " 1", "1 ", "１", "1e2", str(2**63)]
)
def test_ids_reject_noncanonical_or_overflow(value: str) -> None:
    with pytest.raises(ValueError):
        parse_id(value)


def test_bigint_and_name_normalization() -> None:
    assert parse_id(str(2**63 - 1)) == 2**63 - 1
    assert normalize_name("  ＡＤＭＩＮ  ", max_length=100) == "admin"
    assert normalize_name("Straße", max_length=100) == "strasse"
    for value in ("   ", "a\x00b", "a\u200bb", "a" * 101):
        with pytest.raises(ValueError):
            normalize_name(value, max_length=100)


def test_passwords_use_salted_argon2_and_enforce_creation_policy() -> None:
    passwords = Passwords(AuthConfig())
    password = SecretStr("一个足够长度的测试密码phrase")
    digest = passwords.hash(password)
    assert digest.startswith("$argon2id$")
    assert digest != passwords.hash(password)
    assert passwords.verify(password, digest)
    assert not passwords.verify(SecretStr("wrong"), digest)
    assert not passwords.verify(password, None)
    assert not passwords.verify(password, "invalid hash")
    for value in ("short", " " * 15, "x" * 129, "line\nbreak-test"):
        with pytest.raises(ValueError):
            passwords.hash(SecretStr(value))


def test_tokens_only_contain_identity_version_and_expiry() -> None:
    tokens = Tokens(SecretStr("x" * 48), AuthConfig())
    claims = tokens.decode(tokens.issue(2**53 + 1, 3))
    assert claims.sub == str(2**53 + 1)
    assert claims.credential_version == 3
    assert set(claims.model_dump()) == {"sub", "credential_version", "exp"}
    with pytest.raises(ConfigurationError):
        Tokens(SecretStr("too short"), AuthConfig())


@pytest.mark.parametrize(
    "changes",
    [
        {"exp": 1},
        {"sub": "0"},
        {"sub": 123},
        {"credential_version": 0},
        {"credential_version": True},
        {"role": "admin"},
        {"exp": "9999999999"},
    ],
)
def test_signed_invalid_claims_are_rejected(changes: dict) -> None:
    key = "a" * 48
    data = {
        "sub": "1",
        "credential_version": 1,
        "exp": int((datetime.now(UTC) + timedelta(minutes=30)).timestamp()),
    }
    data.update(changes)
    tokens = Tokens(SecretStr(key), AuthConfig())
    with pytest.raises(ValueError):
        tokens.decode(jwt.encode(data, key, algorithm="HS256"))


def test_wrong_signature_algorithm_and_missing_claims() -> None:
    tokens = Tokens(SecretStr("a" * 48), AuthConfig())
    for token in (
        "bad-token",
        Tokens(SecretStr("b" * 48), AuthConfig()).issue(1, 1),
        jwt.encode({"sub": "1"}, "a" * 48, algorithm="HS256"),
        jwt.encode({"sub": "1", "credential_version": 1, "exp": 9999999999}, "", algorithm="none"),
    ):
        with pytest.raises(ValueError):
            tokens.decode(token)
