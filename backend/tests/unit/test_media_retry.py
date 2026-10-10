from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from test_media import configure

from enterprise_mind.ingestion import media_retry
from enterprise_mind.ingestion.media_providers import HTTPVision
from enterprise_mind.rag.providers import ProviderFailure


def configure_transport(tmp_path, monkeypatch, responses):
    provider = HTTPVision(configure(tmp_path, monkeypatch, "vision"))
    client, calls = httpx.AsyncClient, []

    def handle(request):
        calls.append(request)
        response = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handle)),
    )
    sleep = AsyncMock()
    monkeypatch.setattr("enterprise_mind.ingestion.media_providers.asyncio.sleep", sleep)
    monkeypatch.setattr(media_retry.random, "uniform", lambda *_: 0.5)
    return provider, calls, sleep


@pytest.mark.parametrize(
    "status,code,expected,retry",
    [
        (429, "1305", "MEDIA_PROVIDER_BUSY", True),
        (429, 1302, "MEDIA_PROVIDER_RATE_LIMITED", True),
        (429, "Throttling.AllocationQuota", "MEDIA_PROVIDER_RATE_LIMITED", True),
        (429, "insufficient_quota", "MEDIA_PROVIDER_RATE_LIMITED", True),
        (429, "1113", "MEDIA_PROVIDER_QUOTA_EXHAUSTED", False),
        (429, "1310", "MEDIA_PROVIDER_QUOTA_EXHAUSTED", False),
        (400, "Arrearage", "MEDIA_PROVIDER_QUOTA_EXHAUSTED", False),
        (403, "AllocationQuota.FreeTierOnly", "MEDIA_PROVIDER_QUOTA_EXHAUSTED", False),
        (429, "1311", "MEDIA_PROVIDER_REJECTED", False),
        (401, "invalid_api_key", "MEDIA_PROVIDER_REJECTED", False),
        (404, "ModelNotFound", "MEDIA_PROVIDER_REJECTED", False),
    ],
)
async def test_error_categories_and_retryability(
    tmp_path, monkeypatch, status, code, expected, retry
):
    provider, calls, sleep = configure_transport(
        tmp_path,
        monkeypatch,
        [
            httpx.Response(
                status, json={"error": {"code": code, "message": "private provider data"}}
            )
        ],
    )
    with pytest.raises(ProviderFailure) as error:
        await provider.request("/chat/completions", {})
    assert str(error.value) == expected
    assert len(calls) == (2 if retry else 1)
    assert sleep.await_count == int(retry)
    if retry:
        sleep.assert_awaited_once_with(2.5)


@pytest.mark.parametrize("date_header", [False, True])
async def test_retry_after_waits_then_recovers(tmp_path, monkeypatch, date_header):
    header = format_datetime(datetime.now(UTC) + timedelta(seconds=30)) if date_header else "30"
    provider, calls, sleep = configure_transport(
        tmp_path,
        monkeypatch,
        [
            httpx.Response(429, headers={"Retry-After": header}),
            httpx.Response(200, json={"ok": True}),
        ],
    )
    assert await provider.request("/chat/completions", {}) == {"ok": True}
    assert len(calls) == 2 and sleep.await_count == 1
    assert 28 <= sleep.await_args.args[0] <= 30


async def test_long_retry_after_does_not_retry_early(tmp_path, monkeypatch):
    provider, calls, sleep = configure_transport(
        tmp_path, monkeypatch, [httpx.Response(429, headers={"Retry-After": "120"})]
    )
    with pytest.raises(ProviderFailure, match="MEDIA_PROVIDER_RATE_LIMITED"):
        await provider.request("/chat/completions", {})
    assert len(calls) == 1
    sleep.assert_not_awaited()


async def test_exhausted_network_retry_does_not_reuse_previous_rate_limit(tmp_path, monkeypatch):
    provider, calls, sleep = configure_transport(
        tmp_path,
        monkeypatch,
        [httpx.Response(429), httpx.ReadTimeout("private request data")],
    )
    with pytest.raises(ProviderFailure, match="^MEDIA_PROVIDER_UNAVAILABLE$"):
        await provider.request("/chat/completions", {})
    assert len(calls) == 2 and sleep.await_count == 1


@pytest.mark.parametrize("content", [b"not JSON", b"x" * 65537], ids=["invalid-json", "oversized"])
async def test_invalid_error_body_is_bounded_and_uses_http_category(tmp_path, monkeypatch, content):
    provider, calls, sleep = configure_transport(
        tmp_path,
        monkeypatch,
        [httpx.Response(503, content=content), httpx.Response(200, json={"ok": True})],
    )
    assert await provider.request("/chat/completions", {}) == {"ok": True}
    assert len(calls) == 2 and sleep.await_count == 1


def test_backoff_and_malformed_retry_after(monkeypatch):
    monkeypatch.setattr(media_retry.random, "uniform", lambda *_: 0.5)
    assert [media_retry.retry_delay(n, None) for n in range(3)] == [2.5, 4.5, 8.5]
    for value in ["NaN", "Infinity", "-1", "nonsense", "0"]:
        assert media_retry.retry_delay(0, value) == 2.5
