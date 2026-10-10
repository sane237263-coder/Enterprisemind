"""Bounded retries and safe error categories for OCR/Vision HTTP adapters."""

import json
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

_QUOTA_CODES = {
    "1113",
    "1308",
    "1310",
    "1316",
    "1317",
    "1318",
    "1319",
    "1320",
    "1321",
    "Arrearage",
    "AllocationQuota.FreeTierOnly",
    "PrepaidBillOverdue",
    "PostpaidBillOverdue",
}
_REJECTED_CODES = {"1309", "1311", "1313", "1314", "1315", "CommodityNotPurchased"}


async def failure_category(response: httpx.Response) -> tuple[str, bool]:
    # Read only a bounded error envelope; never propagate provider messages or request IDs.
    content = bytearray()
    async for part in response.aiter_bytes():
        if len(content) + len(part) > 64 * 1024:
            break
        content.extend(part)
    code = ""
    try:
        payload = json.loads(content)
        error = payload.get("error", payload) if isinstance(payload, dict) else {}
        value = error.get("code") if isinstance(error, dict) else None
        if type(value) in (str, int) and len(str(value)) <= 128:
            code = str(value)
    except (ValueError, RecursionError):
        pass
    if code in _QUOTA_CODES:
        return "MEDIA_PROVIDER_QUOTA_EXHAUSTED", False
    if code in _REJECTED_CODES:
        return "MEDIA_PROVIDER_REJECTED", False
    if response.status_code == 429:
        # GLM 1305 is model capacity overload, distinct from account rate limits (1302).
        # Aliyun insufficient_quota can mean TPM throttling, so it remains retryable.
        return ("MEDIA_PROVIDER_BUSY" if code == "1305" else "MEDIA_PROVIDER_RATE_LIMITED"), True
    if response.status_code in {408, 500, 502, 503, 504}:
        return "MEDIA_PROVIDER_UNAVAILABLE", True
    return "MEDIA_PROVIDER_REJECTED", False


def retry_delay(attempt: int, retry_after: str | None) -> float | None:
    delay = min(60.0, 2.0 ** (attempt + 1) + random.uniform(0.0, 1.0))
    if not retry_after:
        return delay
    try:
        value = retry_after.strip()
        if value.isascii() and value.isdecimal():
            requested = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return delay
            requested = (date - datetime.now(UTC)).total_seconds()
        # A long cooldown ends this attempt instead of retrying before the server permits it.
        return None if requested > 60.0 else max(delay, requested)
    except (ValueError, OverflowError, TypeError):
        return delay
