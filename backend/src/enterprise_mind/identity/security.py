import hashlib
import hmac
import re
import secrets
import unicodedata
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from enterprise_mind.infrastructure.settings import AuthConfig, ConfigurationError

BIGINT_MAX = 2**63 - 1


def normalize_name(value: str, *, max_length: int) -> str:
    result = unicodedata.normalize("NFKC", value).strip().casefold()
    if (
        not result
        or len(result) > max_length
        or any(unicodedata.category(c)[0] == "C" for c in result)
    ):
        raise ValueError("invalid name")
    return result


def parse_id(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value):
        raise ValueError("invalid identifier")
    number = int(value)
    if number > BIGINT_MAX:
        raise ValueError("identifier out of range")
    return number


class Passwords:
    def __init__(self, config: AuthConfig) -> None:
        self.config = config
        self.hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
        self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))

    def hash(self, password: SecretStr) -> str:
        value = password.get_secret_value()
        if (
            not self.config.password_min_length <= len(value) <= self.config.password_max_length
            or not value.strip()
            or any(c in value for c in ("\x00", "\r", "\n"))
        ):
            raise ValueError("密码不符合配置的长度或字符要求。")
        return self.hasher.hash(value)

    def verify(self, password: SecretStr, password_hash: str | None) -> bool:
        try:
            valid = self.hasher.verify(
                password_hash or self.dummy_hash, password.get_secret_value()
            )
            return valid and password_hash is not None
        except (VerificationError, InvalidHashError):
            return False


class Claims(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sub: str
    credential_version: int
    exp: int


class Tokens:
    def __init__(self, key: SecretStr, config: AuthConfig) -> None:
        if len(key.get_secret_value().encode("utf-8")) < 32:
            raise ConfigurationError("访问令牌签名密钥必须至少 32 字节。")
        self._key = key
        self.ttl_seconds = config.access_token_ttl_seconds

    def digest(self, purpose: str, value: bytes) -> str:
        return hmac.new(
            self._key.get_secret_value().encode(),
            purpose.encode() + b"\x00" + value,
            hashlib.sha256,
        ).hexdigest()

    def issue(self, user_id: int, credential_version: int) -> str:
        return jwt.encode(
            {
                "sub": str(user_id),
                "credential_version": credential_version,
                "exp": int((datetime.now(UTC) + timedelta(seconds=self.ttl_seconds)).timestamp()),
            },
            self._key.get_secret_value(),
            algorithm="HS256",
        )

    def decode(self, token: str) -> Claims:
        try:
            data = jwt.decode(
                token,
                self._key.get_secret_value(),
                algorithms=["HS256"],
                options={"require": ["sub", "credential_version", "exp"]},
            )
            claims = Claims.model_validate(data)
            parse_id(claims.sub)
            if not 1 <= claims.credential_version <= BIGINT_MAX:
                raise ValueError("invalid credential version")
            return claims
        except (jwt.InvalidTokenError, ValidationError, ValueError):
            raise ValueError("无法验证访问令牌。") from None
