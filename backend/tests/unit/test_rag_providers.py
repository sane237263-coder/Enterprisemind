import hashlib
import json

import httpx
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from enterprise_mind.rag.config import EmbeddingConfig, ProvidersConfig, RerankProviderConfig
from enterprise_mind.rag.providers import HTTPEmbeddings, HTTPReranker, ProviderFailure

HTTP_CLIENT = httpx.AsyncClient


@pytest.fixture
def provider_config(tmp_path, monkeypatch):
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer_path = tmp_path / "tokenizer.json"
    tokenizer.save(str(tokenizer_path))
    secret = tmp_path / "key"
    secret.write_text("test-secret", encoding="utf-8")
    monkeypatch.setenv("EM_EMBEDDING_API_KEY_FILE", str(secret))
    monkeypatch.setenv("EM_RERANK_API_KEY_FILE", str(secret))
    return dict(
        provider_key="fixture",
        model_key="fixture-model-v1",
        model="fixture",
        base_url="https://provider.test/v1",
        timeout_seconds=1,
        max_input_tokens=1000,
        tokenizer_file=str(tokenizer_path),
        tokenizer_sha256=hashlib.sha256(tokenizer_path.read_bytes()).hexdigest(),
    )


def install_transport(monkeypatch, handler):
    original = HTTP_CLIENT
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


async def test_embeddings_protocol_reorders_rows_and_retries_only_transient(
    provider_config, monkeypatch
):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/v1/embeddings"
        assert json.loads(request.content)["input"] == ["甲", "乙"]
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    install_transport(monkeypatch, handler)
    provider = HTTPEmbeddings(EmbeddingConfig(**provider_config, dimensions=2))
    assert await provider.embed(["甲", "乙"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert len(calls) == 2
    calls.clear()

    def unauthorized(request):
        calls.append(request)
        return httpx.Response(401, text="credential and source must not appear in error")

    install_transport(monkeypatch, unauthorized)
    with pytest.raises(ProviderFailure, match="PROVIDER_REJECTED") as error:
        await provider.embed(["甲"])
    assert len(calls) == 1
    assert "credential" not in str(error.value)


@pytest.mark.parametrize(
    "response",
    [
        {"data": [{"index": 0, "embedding": [1.0]}]},
        {"data": [{"index": 1, "embedding": [1.0, 0.0]}]},
        {"data": [{"index": 0, "embedding": [0.0, 0.0]}]},
    ],
)
async def test_invalid_embedding_response_never_indexes(provider_config, monkeypatch, response):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=response))
    provider = HTTPEmbeddings(EmbeddingConfig(**provider_config, dimensions=2))
    with pytest.raises(ProviderFailure):
        await provider.embed(["input"])


async def test_rerank_scores_are_model_values_not_query_normalized(provider_config, monkeypatch):
    def handler(request):
        payload = json.loads(request.content)
        assert set(payload) == {"model", "query", "documents", "top_n", "return_documents"}
        assert request.url.path == "/v1/rerank"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": -2.0},
                    {"index": 0, "relevance_score": 3.0},
                ]
            },
        )

    install_transport(monkeypatch, handler)
    provider = HTTPReranker(RerankProviderConfig(**provider_config, score_min=-5.0, score_max=5.0))
    assert await provider.rerank("question", ["a", "b"]) == [3.0, -2.0]


def test_unapproved_provider_origin_is_rejected(provider_config):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProvidersConfig(
            allowed_origins=["https://allowed.test"],
            embedding=EmbeddingConfig(**provider_config, dimensions=2),
        )


async def test_redirect_and_oversized_responses_are_not_followed(provider_config, monkeypatch):
    install_transport(
        monkeypatch,
        lambda request: httpx.Response(302, headers={"Location": "http://internal.test"}),
    )
    provider = HTTPEmbeddings(EmbeddingConfig(**provider_config, dimensions=2))
    with pytest.raises(ProviderFailure, match="PROVIDER_REJECTED"):
        await provider.embed(["input"])
