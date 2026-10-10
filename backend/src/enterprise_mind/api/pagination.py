import base64
import binascii
import hmac
import json
from typing import Any

from pydantic import BaseModel

from enterprise_mind.api.auth import ResponseMeta
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.identity.security import Tokens, parse_id


class PageMeta(ResponseMeta):
    next_cursor: str | None


class Page[T](BaseModel):
    data: list[T]
    meta: PageMeta


class Pagination:
    def __init__(self, tokens: Tokens, scope: dict[str, Any]) -> None:
        self.tokens = tokens
        self.scope = scope

    def encode(self, last_id: int) -> str:
        payload = json.dumps({"id": str(last_id), "scope": self.scope}, sort_keys=True).encode()
        return (
            base64.urlsafe_b64encode(payload).decode() + "." + self.tokens.digest("cursor", payload)
        )

    def decode(self, cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            encoded, signature = cursor.split(".")
            payload = base64.b64decode(encoded, altchars=b"-_", validate=True)
            if not hmac.compare_digest(signature, self.tokens.digest("cursor", payload)):
                raise ValueError("invalid signature")
            data = json.loads(payload)
            if not isinstance(data, dict) or data.get("scope") != self.scope:
                raise ValueError("invalid scope")
            return parse_id(data["id"])
        except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
            raise APIError(ErrorCode.VALIDATION_ERROR) from None
